"""
sync_crm.py
===========
Actualiza el estado de licitaciones vinculadas al CRM Odoo.
Programar: cada 30 minutos (o 1 vez al día si prefieres).

Lee mp_tender_code de crm_projects y sincroniza el estado actual desde MP.
"""

import os, time, json, hashlib, logging, requests, random
from datetime import datetime, timezone
from typing import Optional

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("sync_crm")

MP_TICKET    = os.environ["TICKET_CRM"]
MP_API       = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"
SLEEP        = float(os.getenv("SLEEP_BETWEEN", "2.0"))
CRM_LIMIT    = int(os.getenv("CRM_LIMIT", "2000"))

ESTADOS_FINALES = {"adjudicada","adjudicado","desierta","revocada","revocado"}

T_PROJ  = "crm_projects"
T_LIC   = "crm_mp_licitaciones"
T_ITEMS = "crm_mp_items"
T_ADJ   = "crm_mp_adjudicaciones"

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

def _mp_get(params):
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

def _sb_get(table, filters={}, select="*", limit=5000):
    params = {k: f"eq.{v}" for k, v in filters.items()}
    params.update({"select": select, "limit": str(limit)})
    r = requests.get(f"{SB_REST}/{table}", headers=_sb_headers(), params=params, timeout=30)
    return r.json() if r.ok else []

def _sb_upsert(table, on_conflict, rows):
    if not rows: return
    r = requests.post(f"{SB_REST}/{table}", headers=_sb_headers(),
                      params={"on_conflict": on_conflict} if on_conflict else {},
                      json=rows, timeout=60)
    if not r.ok: log.error("SB %s: %s", table, r.text[:200])

def _sb_delete(table, col, val):
    requests.delete(f"{SB_REST}/{table}", headers=_sb_headers(),
                    params={col: f"eq.{val}"}, timeout=30)

def _hash(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()

def _ts(v):
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

def _should_skip(codigo):
    rows = _sb_get(T_LIC, {"codigo_externo": codigo}, "estado,raw_hash", 1)
    if not rows: return False
    row = rows[0]
    if str(row.get("estado") or "").strip().lower() not in ESTADOS_FINALES: return False
    if not row.get("raw_hash"): return False
    items = _sb_get(T_ITEMS, {"codigo_externo": codigo}, "item_no", 1)
    return bool(items)

def main():
    log.info("=== sync_crm ===")

    # Leer candidatos desde CRM
    proyectos = _sb_get(T_PROJ, select="odoo_id,name,mp_tender_code", limit=CRM_LIMIT)
    seen, candidatos = set(), []
    for p in proyectos:
        code = str(p.get("mp_tender_code") or "").strip()
        if code and code not in seen:
            seen.add(code)
            candidatos.append({"codigo": code, "odoo_id": p.get("odoo_id"), "name": p.get("name")})

    log.info("%d licitaciones CRM a verificar", len(candidatos))
    ok = skip = err = 0

    for cand in candidatos:
        codigo = cand["codigo"]
        if _should_skip(codigo):
            skip += 1
            continue

        try:
            data  = _mp_get({"codigo": codigo})
            time.sleep(SLEEP)
            lics  = data.get("Listado") or []
            if not lics: continue
            lic   = lics[0]

            fechas = lic.get("Fechas") or {}
            comp   = lic.get("Comprador") or {}
            items  = ((lic.get("Items") or {}).get("Listado")) or []
            raw_h  = _hash(lic)

            # Verificar si cambió
            existing = _sb_get(T_LIC, {"codigo_externo": codigo}, "raw_hash", 1)
            if existing and existing[0].get("raw_hash") == raw_h:
                ok += 1
                continue

            cab = {
                "codigo_externo":             codigo,
                "nombre":                     lic.get("Nombre"),
                "tipo":                       lic.get("Tipo"),
                "estado":                     lic.get("Estado"),
                "moneda":                     lic.get("Moneda"),
                "fecha_publicacion":          _ts(fechas.get("FechaPublicacion")),
                "fecha_cierre":              _ts(fechas.get("FechaCierre")),
                "fecha_adjudicacion":         _ts(fechas.get("FechaAdjudicacion")),
                "organismo_nombre":           comp.get("NombreOrganismo"),
                "organismo_region":           comp.get("RegionUnidad"),
                "crm_lead_odoo_id":           cand.get("odoo_id"),
                "crm_lead_name":              cand.get("name"),
                "raw_hash":                   raw_h,
                "last_sync_at":               datetime.now(timezone.utc).isoformat(),
            }
            _sb_upsert(T_LIC, "codigo_externo", [cab])

            # Items y adjudicaciones
            _sb_delete(T_ITEMS, "codigo_externo", codigo)
            _sb_delete(T_ADJ,   "codigo_externo", codigo)

            item_rows, adj_rows = [], []
            seen_i, seen_a = set(), set()
            for it in items:
                if not isinstance(it, dict): continue
                try: correl = int(it.get("Correlativo"))
                except: continue
                if correl in seen_i: continue
                seen_i.add(correl)
                item_rows.append({
                    "codigo_externo":  codigo, "item_no": correl,
                    "nombre_producto": str(it.get("NombreProducto") or "").strip() or None,
                    "cantidad":        _num(it.get("Cantidad")),
                })
                adj = it.get("Adjudicacion")
                for a in (adj if isinstance(adj, list) else ([adj] if isinstance(adj, dict) else [])):
                    if not isinstance(a, dict): continue
                    rut = str(a.get("RutProveedor") or "").strip() or None
                    if not rut: continue
                    key = f"{codigo}|{correl}|{rut}"
                    if key in seen_a: continue
                    seen_a.add(key)
                    adj_rows.append({
                        "codigo_externo":  codigo, "item_no": correl,
                        "proveedor_rut":   rut,
                        "proveedor_nombre":str(a.get("NombreProveedor") or "").strip() or None,
                        "monto_total":     _num(a.get("MontoTotal")),
                        "fecha_resolucion":_ts(a.get("FechaResolucion")),
                    })

            if item_rows: _sb_upsert(T_ITEMS, "codigo_externo,item_no", item_rows)
            if adj_rows:  _sb_upsert(T_ADJ, "codigo_externo,item_no,proveedor_rut", adj_rows)
            log.info("Actualizado: %s | estado=%s", codigo, lic.get("Estado"))
            ok += 1

        except Exception as e:
            log.warning("Error %s: %s", codigo, repr(e))
            err += 1

    log.info("Resultado: ok=%d skip=%d err=%d", ok, skip, err)

if __name__ == "__main__":
    main()
