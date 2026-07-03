"""
sync_activas.py
===============
Busca licitaciones ACTIVAS nuevas o modificadas.
Programar: cada 30 minutos.

Lógica:
  - Trae el listado del día para estado=activas
  - Prefetch de hashes en bulk
  - Solo escribe las que son nuevas o cambiaron
  - Usa cursor para no repetir siempre las mismas
"""

import os, sys, time, json, random, logging, requests
from datetime import datetime, timezone
from typing import Optional

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

from cursor_store import load_cursor, save_cursor

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("sync_activas")

# ── Config ──────────────────────────────────
MP_TICKET    = os.environ["TICKET_ACTIVAS"]
MP_API       = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"

SLEEP        = float(os.getenv("SLEEP_BETWEEN", "2.0"))
MAX_POR_RUN  = int(os.getenv("LIC_MAX_POR_RUN", "100"))   # licitaciones por ejecución
CURSOR_FILE  = os.getenv("LIC_CURSOR_FILE",
               os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cursor_activas.json"))

T_LIC       = "mp_licitaciones"
T_COMP      = "mp_licitacion_comprador"
T_FECHAS    = "mp_licitacion_fechas"
T_ITEMS     = "mp_licitacion_items"
T_ADJ       = "mp_adjudicaciones"

# ── HTTP ─────────────────────────────────────
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
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}

def _sb_upsert(table, on_conflict, rows):
    if not rows: return
    url = f"{SB_REST}/{table}"
    r = requests.post(url, headers=_sb_headers(),
                      params={"on_conflict": on_conflict} if on_conflict else {},
                      json=rows, timeout=60)
    if not r.ok:
        log.error("Supabase %s error: %s", table, r.text[:200])

def _sb_delete(table, col, val):
    requests.delete(f"{SB_REST}/{table}", headers=_sb_headers(),
                    params={col: f"eq.{val}"}, timeout=30)

def _sb_hashes_bulk(codigos: list) -> dict:
    """Retorna dict codigo_externo -> raw_hash de las que ya existen en BD."""
    if not codigos: return {}
    r = requests.get(f"{SB_REST}/{T_LIC}", headers=_sb_headers(),
                     params={"select": "codigo_externo,raw_hash",
                             "codigo_externo": f"in.({','.join(codigos)})",
                             "limit": str(len(codigos)+1)}, timeout=30)
    return {row["codigo_externo"]: row.get("raw_hash")
            for row in (r.json() if r.ok else []) if row.get("codigo_externo")}

# ── Cursor (persistido en Supabase: mp_sync_cursor, key="activas") ───
def _load_cursor() -> int:
    try:
        data = load_cursor("activas", {})
        if data.get("date") == datetime.now().date().isoformat():
            return data.get("pos", 0)
    except Exception: pass
    return 0

def _save_cursor(pos: int):
    save_cursor("activas", {"date": datetime.now().date().isoformat(), "pos": pos})

# ── Parser ───────────────────────────────────
import hashlib

def _hash(obj) -> str:
    return hashlib.md5(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()

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

def _parse(lic: dict):
    from dateutil import parser as dtp
    codigo = str(lic.get("CodigoExterno") or "").strip()
    fechas = lic.get("Fechas") or {}
    comp   = lic.get("Comprador") or {}
    items  = ((lic.get("Items") or {}).get("Listado")) or []

    cab = {
        "codigo_externo":    codigo,
        "nombre":            lic.get("Nombre"),
        "descripcion":       lic.get("Descripcion"),
        "tipo":              lic.get("Tipo"),
        "estado":            lic.get("Estado"),
        "codigo_estado":     lic.get("CodigoEstado"),
        "moneda":            lic.get("Moneda"),
        "raw":               lic,  # monto_estimado es columna GENERADA desde raw->>'MontoEstimado'
        "fecha_publicacion": _ts(fechas.get("FechaPublicacion")),
        "fecha_cierre":      _ts(fechas.get("FechaCierre")),
        "fecha_adjudicacion":_ts(fechas.get("FechaAdjudicacion")),
        "raw_hash":          _hash(lic),
        "last_sync_at":      datetime.now(timezone.utc).isoformat(),
    }
    comp_row = {
        "codigo_externo":   codigo,
        "nombre_organismo": str(comp.get("NombreOrganismo") or "").strip() or None,
        "rut_unidad":       str(comp.get("RutUnidad") or "").strip() or None,
        "nombre_unidad":    str(comp.get("NombreUnidad") or "").strip() or None,
        "region_unidad":    str(comp.get("RegionUnidad") or "").strip() or None,
        "comuna_unidad":    str(comp.get("ComunaUnidad") or "").strip() or None,
    }
    fechas_row = {
        "codigo_externo":      codigo,
        "fecha_publicacion":   _ts(fechas.get("FechaPublicacion")),
        "fecha_cierre":        _ts(fechas.get("FechaCierre")),
        "fecha_adjudicacion":  _ts(fechas.get("FechaAdjudicacion")),
        "fecha_apertura_tecnica": _ts(fechas.get("FechaActoAperturaTecnica")),
    }
    item_rows, adj_rows = [], []
    seen_i, seen_a = set(), set()
    for it in items:
        if not isinstance(it, dict): continue
        try: correl = int(it.get("Correlativo"))
        except: continue
        if correl in seen_i: continue
        seen_i.add(correl)
        item_rows.append({
            "codigo_externo":  codigo, "correlativo": correl,
            "nombre_producto": str(it.get("NombreProducto") or "").strip() or None,
            "categoria":       str(it.get("Categoria") or "").strip() or None,
            "cantidad":        _num(it.get("Cantidad")),
            "unidad_medida":   str(it.get("UnidadMedida") or "").strip() or None,
        })
        adj = it.get("Adjudicacion")
        for a in (adj if isinstance(adj, list) else ([adj] if isinstance(adj, dict) else [])):
            if not isinstance(a, dict): continue
            rut = str(a.get("RutProveedor") or "").strip() or None
            if not rut: continue
            key = f"{codigo}|{correl}|{rut}"
            if key in seen_a: continue
            seen_a.add(key)
            mu  = _num(a.get("MontoUnitario"))
            mt  = _num(a.get("MontoTotal"))
            if mt is None and mu is not None:
                cant = _num(it.get("Cantidad"))
                if cant is not None: mt = mu * cant
            adj_rows.append({
                "licitacion_id":    codigo, "item_no": correl,
                "proveedor_rut":    rut,
                "proveedor_nombre": str(a.get("NombreProveedor") or "").strip() or None,
                "monto_unitario":   mu,
                "monto_total":      mt,
                "moneda":           str(a.get("Moneda") or lic.get("Moneda") or "").strip() or None,
                "fecha_resolucion": _ts(a.get("FechaResolucion")),
            })
    return cab, comp_row, fechas_row, item_rows, adj_rows

# ── Main ─────────────────────────────────────
def main():
    log.info("=== sync_activas ===")

    # 1. Listado de activas
    data    = _mp_get({"estado": "activas"})
    listado = data.get("Listado") or []
    todos   = list({str(l.get("CodigoExterno") or "").strip()
                    for l in listado
                    if str(l.get("CodigoExterno") or "").strip()})

    # 2. Cursor
    cursor  = _load_cursor()
    if cursor >= len(todos):
        cursor = 0
        log.info("Cursor reseteado — nuevo recorrido")
    tramo   = todos[cursor: cursor + MAX_POR_RUN]
    log.info("Activas: %d total, procesando %d (pos %d-%d)",
             len(todos), len(tramo), cursor, cursor + len(tramo))

    # 3. Prefetch raw_hash de las que ya están en BD (para detectar cambios)
    hashes  = _sb_hashes_bulk(tramo)
    log.info("En BD: %d/%d — se procesan todas; se escribe solo si el hash cambió",
             len(hashes), len(tramo))

    # 4. Procesar TODAS las del tramo: inserta nuevas y re-lee abiertas modificadas
    ok = nuevas = actualizadas = sin_cambio = err = 0
    for codigo in tramo:
        try:
            data_det = _mp_get({"codigo": codigo})
            time.sleep(SLEEP)
            lics = data_det.get("Listado") or []
            if not lics: continue
            lic = lics[0]
            cab, comp_row, fechas_row, item_rows, adj_rows = _parse(lic)

            # Si ya está en BD y el hash no cambió, no reescribimos (ahorra writes)
            prev_hash = hashes.get(codigo)
            if prev_hash and prev_hash == cab["raw_hash"]:
                sin_cambio += 1
                continue

            _sb_upsert(T_LIC,    "codigo_externo", [cab])
            _sb_upsert(T_COMP,   "codigo_externo", [comp_row])
            _sb_upsert(T_FECHAS, "codigo_externo", [fechas_row])
            if item_rows: _sb_upsert(T_ITEMS, "codigo_externo,correlativo", item_rows)
            if adj_rows:  _sb_upsert(T_ADJ, "licitacion_id,item_no,proveedor_rut", adj_rows)

            if codigo in hashes: actualizadas += 1
            else:                nuevas += 1
            ok += 1

        except Exception as e:
            log.warning("Error %s: %s", codigo, repr(e))
            err += 1

    _save_cursor(cursor + MAX_POR_RUN)  # avanza aunque no haya cambios
    log.info("Resultado: %d nuevas, %d actualizadas, %d sin cambio, %d errores | cursor→%d",
             nuevas, actualizadas, sin_cambio, err, cursor + MAX_POR_RUN)

if __name__ == "__main__":
    main()