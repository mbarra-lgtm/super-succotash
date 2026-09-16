"""
sync_estados_diario.py
======================
Trae todas las licitaciones que cambiaron de estado HOY (adjudicada, desierta,
revocada, cerrada) y actualiza mp_licitaciones + mp_adjudicaciones para las
que ya existen en BD.

Programar: 1 vez al día, ej. 06:00 AM (después que la API actualice).
NO inserta licitaciones nuevas — solo actualiza las conocidas.
"""

import os, time, json, hashlib, logging, requests
from datetime import datetime, timezone, date, timedelta
from typing import Optional

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

# Después de load_dotenv: sb_client lee las credenciales al importarse.
import sb_client as sb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("sync_estados_diario")

# ── Config ──────────────────────────────────────────────────────────────────
MP_TICKET    = os.environ["TICKET_ACTIVAS"]          # reutiliza el mismo ticket
MP_API       = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"

SLEEP        = float(os.getenv("SLEEP_BETWEEN", "2.0"))
# Cuántos días atrás revisar (por si la tarea no corrió ayer)
DIAS_ATRAS   = int(os.getenv("ESTADOS_DIAS_ATRAS", "2"))

ESTADOS_OBJETIVO = ["adjudicada", "desierta", "revocada", "cerrada"]

T_LIC = "mp_licitaciones"
T_ADJ = "mp_adjudicaciones"

# ── HTTP ────────────────────────────────────────────────────────────────────
_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

def _mp_get(params: dict) -> dict:
    r = _session.get(MP_API, params={**params, "ticket": MP_TICKET}, timeout=45)
    if r.status_code == 429:
        log.warning("429 — esperando 60s...")
        time.sleep(60)
        r = _session.get(MP_API, params={**params, "ticket": MP_TICKET}, timeout=45)
    r.raise_for_status()
    return r.json()

def _sb_headers():
    return {
        "apikey": SB_KEY,
        "Authorization": f"Bearer {SB_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal"
    }

_sb_upsert = sb.upsert

def _sb_exists_bulk(codigos: list) -> set:
    """Retorna set de códigos que YA existen en mp_licitaciones.

    El `if r.ok:` sin else que había acá era un fallo silencioso con dientes: un
    chunk que fallaba se saltaba sin ruido y el set volvía incompleto, así que
    licitaciones que SÍ estaban en BD se trataban como inexistentes. Ahora un
    chunk que no se puede leer aborta la corrida en vez de devolver una respuesta
    a medias.
    """
    filas = sb.select_in(T_LIC, "codigo_externo", "codigo_externo", codigos, chunk=100)
    return {f["codigo_externo"] for f in filas}

def _sb_delete_adj(licitacion_id: str):
    sb.delete(T_ADJ, {"licitacion_id": f"eq.{licitacion_id}"})

def _reemplazar_adj(licitacion_id: str, adj_rows: list):
    """Deja en BD exactamente las adjudicaciones recién leídas.

    Antes se borraba todo y después se insertaba: si el insert fallaba, esa
    licitación quedaba sin adjudicaciones y nada las reponía. Ahora se escribe
    primero y sólo después se borran las que sobraron, así que en ningún momento
    la licitación queda vacía. Las bajas reales (una adjudicación que MP eliminó)
    siguen propagándose, que es para lo que existía el delete.
    """
    previas = sb.select(T_ADJ, {"select": "item_no,proveedor_rut",
                                "licitacion_id": f"eq.{licitacion_id}"})
    sb.upsert(T_ADJ, "licitacion_id,item_no,proveedor_rut", adj_rows)

    vivas = {(str(r.get("item_no")), str(r.get("proveedor_rut"))) for r in adj_rows}
    for p in previas:
        if (str(p.get("item_no")), str(p.get("proveedor_rut"))) not in vivas:
            sb.delete(T_ADJ, {"licitacion_id": f"eq.{licitacion_id}",
                              "item_no": f"eq.{p['item_no']}",
                              "proveedor_rut": f"eq.{p['proveedor_rut']}"})

# ── Parsers ─────────────────────────────────────────────────────────────────
def _ts(v) -> Optional[str]:
    if not v: return None
    try:
        from dateutil import parser as dtp
        dt = dtp.parse(str(v))
        if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except: return None

def _num(v):
    try: return float(str(v).replace(",", ".").strip())
    except: return None

def _hash(obj) -> str:
    return hashlib.md5(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()

def _parse_detalle(lic: dict) -> tuple:
    """Devuelve (estado_row, adj_rows) para actualizar en BD."""
    codigo = str(lic.get("CodigoExterno") or "").strip()
    fechas = lic.get("Fechas") or {}
    items  = ((lic.get("Items") or {}).get("Listado")) or []

    estado_row = {
        "codigo_externo":     codigo,
        "estado":             lic.get("Estado"),
        "codigo_estado":      lic.get("CodigoEstado"),
        "fecha_adjudicacion": _ts(fechas.get("FechaAdjudicacion")),
        "raw_hash":           _hash(lic),
        "last_sync_at":       datetime.now(timezone.utc).isoformat(),
        "last_detail_fetch_at": datetime.now(timezone.utc).isoformat(),
    }

    adj_rows = []
    seen = set()
    for it in items:
        if not isinstance(it, dict): continue
        try: correl = int(it.get("Correlativo"))
        except: continue
        adj = it.get("Adjudicacion")
        for a in (adj if isinstance(adj, list) else ([adj] if isinstance(adj, dict) else [])):
            if not isinstance(a, dict): continue
            rut = str(a.get("RutProveedor") or "").strip() or None
            if not rut: continue
            key = f"{codigo}|{correl}|{rut}"
            if key in seen: continue
            seen.add(key)
            # La API de MP no devuelve MontoTotal: sin este fallback el job diario
            # deja monto_total en NULL y, como borra antes de escribir, destruye lo
            # que backfill_estados_lic.py / refetch_adjudicaciones.py acababan de
            # reparar. Mismo parser que esos dos, a proposito.
            mu   = _num(a.get("MontoUnitario"))
            cant = _num(a.get("Cantidad"))
            if cant is None:
                cant = _num(it.get("Cantidad"))
            mt = _num(a.get("MontoTotal"))
            fuente = "api_monto_total" if mt is not None else None
            if mt is None and mu is not None and cant is not None:
                mt, fuente = mu * cant, "api_mu_x_cantidad"
            adj_rows.append({
                "licitacion_id":    codigo,
                "item_no":          correl,
                "proveedor_rut":    rut,
                "proveedor_nombre": str(a.get("NombreProveedor") or "").strip() or None,
                "cantidad":         cant,
                "monto_unitario":   mu,
                "monto_total":      mt,
                "monto_total_fuente": fuente,
                "moneda":           str(a.get("Moneda") or lic.get("Moneda") or "").strip() or None,
                "fecha_resolucion": _ts(a.get("FechaResolucion")),
                "nro_resolucion":   str(a.get("NroResolucion") or "").strip() or None,
            })

    return estado_row, adj_rows

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    log.info("=== sync_estados_diario ===")

    # 1. Recopilar listados de los últimos DIAS_ATRAS días × cada estado objetivo
    candidatos: dict[str, str] = {}  # codigo → estado (básico del listado)

    for dias in range(DIAS_ATRAS):
        dia = date.today() - timedelta(days=dias)
        fecha_str = dia.strftime("%d%m%Y")
        for estado in ESTADOS_OBJETIVO:
            try:
                data    = _mp_get({"fecha": fecha_str, "estado": estado})
                listado = data.get("Listado") or []
                nuevos  = 0
                for item in listado:
                    codigo = str(item.get("CodigoExterno") or "").strip()
                    if codigo and codigo not in candidatos:
                        candidatos[codigo] = estado
                        nuevos += 1
                log.info("  %s / %s → %d licitaciones", dia, estado, nuevos)
                time.sleep(0.5)
            except Exception as e:
                log.warning("  Error %s/%s: %s", dia, estado, repr(e))

    log.info("Total candidatos únicos: %d", len(candidatos))
    if not candidatos:
        log.info("Nada que procesar.")
        return

    # 2. Filtrar solo los que ya existen en BD
    todos_codigos = list(candidatos.keys())
    en_bd = _sb_exists_bulk(todos_codigos)
    a_actualizar = [c for c in todos_codigos if c in en_bd]
    ignorados    = len(todos_codigos) - len(a_actualizar)
    log.info("En BD: %d | Ignorados (no conocidos): %d", len(a_actualizar), ignorados)

    # 3. Para cada uno, traer detalle y actualizar
    ok = sin_cambio = err = 0
    for codigo in a_actualizar:
        try:
            data = _mp_get({"codigo": codigo})
            time.sleep(SLEEP)
            lics = data.get("Listado") or []
            if not lics:
                log.warning("Sin detalle para %s", codigo)
                continue

            estado_row, adj_rows = _parse_detalle(lics[0])

            # Actualizar estado en mp_licitaciones
            _sb_upsert(T_LIC, "codigo_externo", [estado_row])

            # Reemplazar adjudicaciones si las hay
            if adj_rows:
                _reemplazar_adj(codigo, adj_rows)

            log.info("✓ %s → %s (%d adj)", codigo, estado_row["estado"], len(adj_rows))
            ok += 1

        except Exception as e:
            log.warning("✗ %s: %s", codigo, repr(e))
            err += 1

    log.info("=== Resultado: ok=%d sin_cambio=%d err=%d ===", ok, sin_cambio, err)

    # Rojo si se perdió alguna escritura hacia Supabase.
    sb.exit_si_hubo_fallos()

if __name__ == "__main__":
    main()
