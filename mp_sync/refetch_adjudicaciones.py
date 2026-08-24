"""
refetch_adjudicaciones.py
=========================
Re-fetch dirigido del detalle de licitaciones cuyas adjudicaciones quedaron sin
monto por el bug de `backfill_estados_lic.py` (no aplicaba el fallback
MontoUnitario x Cantidad; la API de MP no devuelve MontoTotal).

Por qué hace falta un script aparte: `backfill_estados_lic.py` selecciona
licitaciones con `estado is null`, o sea nunca vuelve a las que ya procesó.
Las afectadas por el bug SI tienen estado — solo les falta el monto. Este
script las direcciona explícitamente.

El parser se reusa tal cual de backfill_estados_lic (ya parchado), así que las
filas quedan con cantidad, monto_unitario, monto_total y monto_total_fuente
consistentes con lo que escribe la ingesta normal.

Selección de objetivos, por prioridad (env `REFETCH_SCOPE`):
  bti        (default) licitaciones donde participo el grupo, via
             crm_projects.mp_tender_code. Son ~167 y es lo que desbloquea el
             analisis de brecha de precio.
  sin_monto  cualquier licitacion con filas en mp_adjudicaciones sin
             monto_unitario. Son ~289k filas -> muchisimas licitaciones;
             usar con REFETCH_MAX y dejar que drene en varias noches.
  sin_acta   licitaciones del grupo (via crm_projects.mp_tender_code) que YA
             tienen estado en el espejo pero CERO filas en mp_adjudicaciones.
             Es la zona ciega entre los dos backfills: backfill_estados_lic.py
             solo toma licitaciones con estado null y el scope bti solo las que
             tienen filas sin monto, asi que una licitacion adjudicada cuya acta
             nunca se ingesto no califica en ninguno de los dos y desaparece de
             todo reporte de adjudicaciones (caso 5240-207-LR24, adjudicada el
             05-06-2025). Se limita a estado Adjudicada o Cerrada: Desierta,
             Revocada y Publicada no tienen acta que traer.
  lista      los codigos de REFETCH_CODIGOS, separados por coma.

Config:
  REFETCH_SCOPE    bti | sin_monto | lista        (default: bti)
  REFETCH_CODIGOS  codigos separados por coma     (solo con scope=lista)
  REFETCH_MAX      tope de licitaciones por corrida (default: 400)
  SLEEP_BETWEEN    segundos entre llamadas        (default: 2.0)

Requiere TICKET_ACTIVAS, SUPABASE_URL, SUPABASE_SERVICE_KEY.
Idempotente: re-correrlo sobre las mismas licitaciones solo las reescribe.
"""

import os, re, sys, time, logging, requests
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

# Reusa el parser y los helpers ya parchados.
from backfill_estados_lic import (
    _mp_get, _parse_detalle, _sb_upsert, _sb_delete_adj, _sb_headers,
    SB_REST, T_LIC, T_ADJ, SLEEP,
)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("refetch_adj")

SCOPE   = (os.getenv("REFETCH_SCOPE") or "bti").strip().lower()
MAX_LIC = int(os.getenv("REFETCH_MAX", "400"))

# Tipos de proceso que NO son licitacion: licitaciones.json les responde 500 y,
# como el 500 corta el sleep del loop, cada uno desencadenaba un 429 con 90s de
# castigo. Se excluyen por el sufijo del codigo (…-RFI25, …-COT26, …-SC24).
# Fail-open: un tipo desconocido se intenta igual, para no descartar en silencio
# una licitacion real.
TIPOS_NO_LICITACION = {t.strip().upper() for t in
    (os.getenv("REFETCH_EXCLUIR_TIPOS") or "RFI,COT,SC,FTD,AG,CM,TD").split(",") if t.strip()}

_RE_TIPO = re.compile(r"-([A-Z]{1,3}[0-9]?)([0-9]{2})$")


def _tipo_de_codigo(cod: str) -> str:
    m = _RE_TIPO.match(cod[cod.rfind("-"):]) if "-" in cod else None
    return (m.group(1).upper() if m else "")


def _solo_licitaciones(codigos):
    """Descarta RFI, cotizaciones de compra agil y otros procesos que no son licitacion."""
    out, descartados = [], {}
    for c in codigos:
        t = _tipo_de_codigo(c)
        if t in TIPOS_NO_LICITACION:
            descartados[t] = descartados.get(t, 0) + 1
        else:
            out.append(c)
    if descartados:
        log.info("Descartados por tipo (no son licitacion): %s",
                 ", ".join(f"{k}={v}" for k, v in sorted(descartados.items())))
    return out


def _sb_get(path, params):
    r = requests.get(f"{SB_REST}/{path}", headers=_sb_headers(),
                     params=params, timeout=45)
    r.raise_for_status()
    return r.json()


def _codigos_crm():
    """Todos los códigos de licitación que el CRM registra para el grupo."""
    rows = _sb_get("crm_projects", {
        "select": "mp_tender_code",
        "mp_tender_code": "not.is.null",
        "is_active": "not.is.false",
        "limit": "5000",
    })
    return sorted({r["mp_tender_code"].strip() for r in rows if (r.get("mp_tender_code") or "").strip()})


def _codigos_de_nuestras_oc():
    """Licitaciones que aparecen en las OC recibidas por el grupo.

    Hace falta porque el CRM no tiene todas: hay licitaciones de 2024 y comienzos
    de 2025 que ganamos y de las que solo queda rastro en la orden de compra. La
    OC es prueba de adjudicacion, asi que su codigo_licitacion es un objetivo
    legitimo para traer el acta.
    """
    ruts = os.getenv("REFETCH_RUTS_GRUPO", "87.927.900-3,77.712.689-K,76.708.952-K")
    lista = ",".join(f'"{r.strip()}"' for r in ruts.split(",") if r.strip())
    rows = _sb_get("mp_oc_header", {
        "select": "codigo_licitacion",
        "codigo_licitacion": "not.is.null",
        "proveedor_rut": f"in.({lista})",
        "limit": "5000",
    })
    return sorted({r["codigo_licitacion"].strip() for r in rows if (r.get("codigo_licitacion") or "").strip()})


def _codigos_bti():
    """Códigos de licitación donde participó el grupo y falta el monto."""
    codigos = set(_codigos_crm())
    if not codigos:
        return []

    # Nos quedamos solo con las que tienen adjudicaciones sin monto_unitario:
    # las que ya se resolvieron por SQL no necesitan llamada a la API.
    pendientes = set()
    codigos = sorted(codigos)
    for i in range(0, len(codigos), 80):
        chunk = codigos[i:i + 80]
        lista = ",".join(f'"{c}"' for c in chunk)
        rows = _sb_get(T_ADJ, {
            "select": "licitacion_id",
            "licitacion_id": f"in.({lista})",
            "monto_unitario": "is.null",
            "limit": "10000",
        })
        pendientes.update(r["licitacion_id"] for r in rows)
    return sorted(pendientes)


def _codigos_sin_acta():
    """Licitaciones del grupo con estado en el espejo y ninguna fila de acta.

    Dos consultas: primero se descartan las que ya tienen filas en
    mp_adjudicaciones, y de las que quedan se piden solo las que el espejo marca
    Adjudicada o Cerrada. "Cerrada" entra a proposito: son licitaciones cuyo
    estado nunca se refresco despues del cierre y pueden estar adjudicadas sin
    que el espejo lo sepa; la llamada al detalle actualiza estado y acta de una.
    """
    codigos = sorted(set(_codigos_crm()) | set(_codigos_de_nuestras_oc()))
    if not codigos:
        return []

    con_acta = set()
    for i in range(0, len(codigos), 80):
        chunk = codigos[i:i + 80]
        lista = ",".join(f'"{c}"' for c in chunk)
        rows = _sb_get(T_ADJ, {
            "select": "licitacion_id",
            "licitacion_id": f"in.({lista})",
            "limit": "20000",
        })
        con_acta.update(r["licitacion_id"] for r in rows)

    faltan = [c for c in codigos if c not in con_acta]
    if not faltan:
        return []

    # De las que faltan, entran dos casos:
    #  a) tienen cabecera y el espejo las marca Adjudicada o Cerrada;
    #  b) NO tienen cabecera en mp_licitaciones. Pasa con las licitaciones cerradas
    #     antes de que arrancara sync_activas.py, que solo ve las activas del dia:
    #     nunca las vio y por eso no hay fila. La API responde por codigo igual, asi
    #     que se piden lo mismo (es el caso de 2924-35-LP24, 5420-26/27-LR24, etc.).
    # Quedan fuera Desierta, Revocada y Publicada: no hay acta que traer.
    con_cabecera, elegibles = set(), []
    for i in range(0, len(faltan), 80):
        chunk = faltan[i:i + 80]
        lista = ",".join(f'"{c}"' for c in chunk)
        rows = _sb_get(T_LIC, {
            "select": "codigo_externo,estado",
            "codigo_externo": f"in.({lista})",
            "limit": "5000",
        })
        for r in rows:
            con_cabecera.add(r["codigo_externo"])
            if (r.get("estado") or "") in ("Adjudicada", "Cerrada"):
                elegibles.append(r["codigo_externo"])
    sin_cabecera = [c for c in faltan if c not in con_cabecera]
    out = _solo_licitaciones(sorted(set(elegibles) | set(sin_cabecera)))
    log.info("sin_acta: %d códigos (CRM + OC del grupo), %d con acta, "
             "%d objetivos (%d con cabecera adjudicada/cerrada, %d sin cabecera)",
             len(codigos), len(con_acta), len(out), len(elegibles), len(sin_cabecera))
    return out


def _codigos_sin_monto():
    """Cualquier licitación con adjudicaciones sin monto_unitario."""
    rows = _sb_get(T_ADJ, {
        "select": "licitacion_id",
        "monto_unitario": "is.null",
        "order": "licitacion_id.asc",
        "limit": str(MAX_LIC * 20),
    })
    vistos, out = set(), []
    for r in rows:
        c = r["licitacion_id"]
        if c not in vistos:
            vistos.add(c); out.append(c)
    return out


def objetivos():
    if SCOPE == "lista":
        raw = os.getenv("REFETCH_CODIGOS") or ""
        return [c.strip() for c in raw.split(",") if c.strip()]   # lista explicita: no se filtra
    if SCOPE == "sin_monto":
        return _codigos_sin_monto()
    if SCOPE == "bti":
        return _codigos_bti()
    if SCOPE == "sin_acta":
        return _codigos_sin_acta()
    log.error("REFETCH_SCOPE desconocido: %r", SCOPE)
    sys.exit(2)


def main():
    codigos = objetivos()
    if not codigos:
        log.info("Nada que hacer: sin licitaciones objetivo para scope=%s.", SCOPE)
        return

    lote = codigos[:MAX_LIC]
    log.info("=== refetch_adjudicaciones === scope=%s | %d objetivos | corriendo %d (tope %d)",
             SCOPE, len(codigos), len(lote), MAX_LIC)

    ok = err = sin_detalle = con_monto = 0

    for i, codigo in enumerate(lote, 1):
        try:
            data = _mp_get({"codigo": codigo})
            lics = data.get("Listado") or []
            if not lics:
                _sb_upsert(T_LIC, "codigo_externo", [{
                    "codigo_externo":       codigo,
                    "last_detail_fetch_at": datetime.now(timezone.utc).isoformat(),
                }])
                sin_detalle += 1
                continue

            estado_row, adj_rows = _parse_detalle(lics[0])
            _sb_upsert(T_LIC, "codigo_externo", [estado_row])
            if adj_rows:
                _sb_delete_adj(codigo)
                _sb_upsert(T_ADJ, "licitacion_id,item_no,proveedor_rut", adj_rows)
                con_monto += sum(1 for r in adj_rows if r.get("monto_total") is not None)
            ok += 1

            if i % 25 == 0:
                log.info("  [%d/%d] %s | %d filas con monto acumuladas",
                         i, len(lote), codigo, con_monto)

        except Exception as e:
            log.warning("✗ %s: %s", codigo, repr(e))
            err += 1
            # Marcar el intento fallido: un 500 permanente (proceso que no existe en
            # licitaciones.json) no debe volver a encabezar el lote cada noche.
            try:
                _sb_upsert(T_LIC, "codigo_externo", [{
                    "codigo_externo":       codigo,
                    "last_detail_fetch_at": datetime.now(timezone.utc).isoformat(),
                }])
            except Exception:
                pass
        finally:
            # SIEMPRE, no solo cuando la llamada funciona: sin esto un 500 saltaba el
            # sleep y la rafaga siguiente se comia un 429 con 90 s de castigo.
            time.sleep(SLEEP)

    restantes = max(0, len(codigos) - len(lote))
    log.info("=== Fin: %d ok, %d sin detalle, %d errores | %d filas de adjudicación con monto | "
             "%d licitaciones quedan para la próxima corrida ===",
             ok, sin_detalle, err, con_monto, restantes)

    try:
        _sb_upsert("data_freshness", "dataset", [{
            "dataset":      f"mp_adjudicaciones_refetch_{SCOPE}",
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "rows_changed": con_monto,
            "source":       "refetch_adjudicaciones.py",
        }])
    except Exception as e:
        log.warning("No pude estampar data_freshness: %s", repr(e))


if __name__ == "__main__":
    main()
