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
  lista      los codigos de REFETCH_CODIGOS, separados por coma.

Config:
  REFETCH_SCOPE    bti | sin_monto | lista        (default: bti)
  REFETCH_CODIGOS  codigos separados por coma     (solo con scope=lista)
  REFETCH_MAX      tope de licitaciones por corrida (default: 400)
  SLEEP_BETWEEN    segundos entre llamadas        (default: 2.0)

Requiere TICKET_ACTIVAS, SUPABASE_URL, SUPABASE_SERVICE_KEY.
Idempotente: re-correrlo sobre las mismas licitaciones solo las reescribe.
"""

import os, sys, time, logging, requests
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


def _sb_get(path, params):
    r = requests.get(f"{SB_REST}/{path}", headers=_sb_headers(),
                     params=params, timeout=45)
    r.raise_for_status()
    return r.json()


def _codigos_bti():
    """Códigos de licitación donde participó el grupo y falta el monto."""
    rows = _sb_get("crm_projects", {
        "select": "mp_tender_code",
        "mp_tender_code": "not.is.null",
        "is_active": "not.is.false",
        "limit": "5000",
    })
    codigos = {r["mp_tender_code"].strip() for r in rows if (r.get("mp_tender_code") or "").strip()}
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
        return [c.strip() for c in raw.split(",") if c.strip()]
    if SCOPE == "sin_monto":
        return _codigos_sin_monto()
    if SCOPE == "bti":
        return _codigos_bti()
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
            time.sleep(SLEEP)
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
