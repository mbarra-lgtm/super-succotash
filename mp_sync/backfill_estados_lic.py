"""
backfill_estados_lic.py
=======================
Recorre licitaciones en mp_licitaciones que tienen estado null (codigo_estado=5)
y fecha_cierre ya pasada, consulta el detalle a la API y actualiza estado +
adjudicaciones.

Diseñado para correr en la noche (~22:00), procesa un lote por ejecución
y guarda cursor para retomar donde quedó.

Estimado: ~37k licitaciones × 2s = ~20h total → ~2-3 semanas de noches.
"""

import os, time, json, hashlib, logging, requests
from datetime import datetime, timezone
from typing import Optional

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

from cursor_store import load_cursor, save_cursor

# La carpeta de logs debe existir ANTES de configurar el FileHandler
os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "logs", f"backfill_estados_{datetime.now().strftime('%Y%m%d')}.log"),
            encoding="utf-8"
        )
    ]
)
log = logging.getLogger("backfill_estados")

# ── Config ──────────────────────────────────────────────────────────────────
MP_TICKET    = os.environ["TICKET_ACTIVAS"]
MP_API       = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"

SLEEP        = float(os.getenv("SLEEP_BETWEEN", "2.0"))
# Cuántas licitaciones procesar por ejecución nocturna
# A 2s c/u: 1000 = ~33min | 1500 = ~50min | 2000 = ~67min
LOTE_SIZE    = int(os.getenv("BACKFILL_LOTE", "1500"))

CURSOR_FILE  = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".cursor_backfill_estados.json"
)

T_LIC = "mp_licitaciones"
T_ADJ = "mp_adjudicaciones"

# ── HTTP ────────────────────────────────────────────────────────────────────
_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

def _mp_get(params: dict) -> dict:
    r = _session.get(MP_API, params={**params, "ticket": MP_TICKET}, timeout=45)
    if r.status_code == 429:
        log.warning("429 — esperando 90s...")
        time.sleep(90)
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

def _sb_upsert(table: str, on_conflict: str, rows: list):
    if not rows: return
    r = requests.post(
        f"{SB_REST}/{table}", headers=_sb_headers(),
        params={"on_conflict": on_conflict} if on_conflict else {},
        json=rows, timeout=60
    )
    if not r.ok:
        log.error("SB %s error: %s", table, r.text[:300])

def _sb_delete_adj(licitacion_id: str):
    requests.delete(
        f"{SB_REST}/{T_ADJ}", headers=_sb_headers(),
        params={"licitacion_id": f"eq.{licitacion_id}"}, timeout=30
    )

# ── Cursor ──────────────────────────────────────────────────────────────────
def _load_cursor() -> dict:
    try:
        data = load_cursor("backfill_estados", {})
        if data: return data
    except: pass
    return {"offset": 0, "procesadas": 0, "ok": 0, "err": 0, "sin_detalle": 0}

def _save_cursor(state: dict):
    save_cursor("backfill_estados", state)

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
    try: return float(str(v).strip())
    except: return None

def _hash(obj) -> str:
    return hashlib.md5(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()

def _parse_detalle(lic: dict) -> tuple:
    codigo = str(lic.get("CodigoExterno") or "").strip()
    fechas = lic.get("Fechas") or {}
    items  = ((lic.get("Items") or {}).get("Listado")) or []

    estado_row = {
        "codigo_externo":       codigo,
        "estado":               lic.get("Estado"),
        "codigo_estado":        lic.get("CodigoEstado"),
        "fecha_adjudicacion":   _ts(fechas.get("FechaAdjudicacion")),
        "raw_hash":             _hash(lic),
        "last_sync_at":         datetime.now(timezone.utc).isoformat(),
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
            adj_rows.append({
                "licitacion_id":    codigo,
                "item_no":          correl,
                "proveedor_rut":    rut,
                "proveedor_nombre": str(a.get("NombreProveedor") or "").strip() or None,
                "monto_unitario":   _num(a.get("MontoUnitario")),
                "monto_total":      _num(a.get("MontoTotal")),
                "moneda":           str(a.get("Moneda") or lic.get("Moneda") or "").strip() or None,
                "fecha_resolucion": _ts(a.get("FechaResolucion")),
                "nro_resolucion":   str(a.get("NroResolucion") or "").strip() or None,
            })

    return estado_row, adj_rows

# ── Fetch pendientes desde Supabase ─────────────────────────────────────────
def _get_pendientes(offset: int, limit: int) -> list:
    """
    Trae licitaciones con estado null y fecha_cierre pasada,
    ordenadas por fecha_cierre ASC (las más antiguas primero).
    """
    r = requests.get(
        f"{SB_REST}/{T_LIC}", headers=_sb_headers(),
        params={
            "select":        "codigo_externo,fecha_cierre",
            "codigo_estado":  "eq.5",
            "estado":         "is.null",
            "fecha_cierre":   "lt.now()",
            "order":          "fecha_cierre.asc",
            "limit":          str(limit),
            "offset":         str(offset),
        }, timeout=30
    )
    return r.json() if r.ok else []

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    # Asegurar carpeta logs
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(logs_dir, exist_ok=True)

    state = _load_cursor()
    log.info("=== backfill_estados_lic === offset=%d | acumulado: ok=%d err=%d",
             state["offset"], state["ok"], state["err"])

    pendientes = _get_pendientes(state["offset"], LOTE_SIZE)
    if not pendientes:
        log.info("✅ Backfill completo — no hay más pendientes con estado null.")
        return

    log.info("Lote: %d licitaciones (offset %d → %d)",
             len(pendientes), state["offset"], state["offset"] + len(pendientes))

    ok = err = sin_detalle = 0

    for i, row in enumerate(pendientes, 1):
        codigo = row["codigo_externo"]
        try:
            data = _mp_get({"codigo": codigo})
            time.sleep(SLEEP)
            lics = data.get("Listado") or []
            if not lics:
                log.debug("Sin detalle: %s", codigo)
                # Igual marcamos que se consultó para no repetir
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

            if i % 50 == 0:
                log.info("  [%d/%d] Último: %s → %s (%d adj)",
                         i, len(pendientes), codigo,
                         estado_row.get("estado") or "?", len(adj_rows))
            ok += 1

        except Exception as e:
            log.warning("✗ %s: %s", codigo, repr(e))
            err += 1

    # Actualizar cursor
    state["offset"]      += len(pendientes)
    state["procesadas"]  += len(pendientes)
    state["ok"]          += ok
    state["err"]         += err
    state["sin_detalle"] += sin_detalle
    state["ultimo_run"]   = datetime.now().isoformat()
    _save_cursor(state)

    log.info("=== Lote terminado: ok=%d sin_detalle=%d err=%d | Total acumulado: %d/%d ===",
             ok, sin_detalle, err, state["procesadas"],
             state["procesadas"] + max(0, 37267 - state["offset"]))

if __name__ == "__main__":
    main()
