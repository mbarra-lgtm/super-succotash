"""
backfill_noche.py
=================
Rellena el universo completo de licitaciones históricas.
Programar: 1 vez al día a las 22:00.

Hace 3 cosas en secuencia:
  1. Licitaciones históricas por fecha (día por día hacia atrás)
  2. Compras Ágiles sin detail_synced_at
  3. OC sin total_neto
"""

import os, time, json, hashlib, logging, requests, random
from datetime import datetime, timezone, timedelta, date
from typing import Optional

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("backfill")

MP_TICKET    = os.environ["TICKET_BACKFILL"]
MP_API_V1    = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"
MP_API_V2    = "https://api2.mercadopublico.cl"
MP_OC_API    = "https://api.mercadopublico.cl/servicios/v1/publico/ordenesdecompra.json"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"

SLEEP              = float(os.getenv("SLEEP_BETWEEN_BACKFILL", "2.0"))
DIAS_HISTORICO_LIC = int(os.getenv("BACKFILL_DIAS_LIC", "365"))
BATCH_CA           = int(os.getenv("BACKFILL_BATCH_CA", "100"))
BATCH_OC           = int(os.getenv("BACKFILL_BATCH_OC", "50"))
CHECKPOINT_FILE    = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".backfill_checkpoint.json")

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

# ── Helpers HTTP ─────────────────────────────
def _get_v1(params):
    r = _session.get(MP_API_V1, params={**params, "ticket": MP_TICKET}, timeout=45)
    if r.status_code == 429:
        log.warning("429 v1 — esperando 60s..."); time.sleep(60)
        r = _session.get(MP_API_V1, params={**params, "ticket": MP_TICKET}, timeout=45)
    r.raise_for_status(); return r.json()

def _get_v2(path, params):
    r = _session.get(f"{MP_API_V2}{path}", headers={"ticket": MP_TICKET},
                     params=params, timeout=45)
    if r.status_code == 429:
        log.warning("429 v2 — esperando 60s..."); time.sleep(60)
        r = _session.get(f"{MP_API_V2}{path}", headers={"ticket": MP_TICKET},
                         params=params, timeout=45)
    r.raise_for_status()
    data = r.json()
    if data.get("success") != "OK": raise RuntimeError(str(data.get("errors")))
    return data["payload"]

# ── Helpers Supabase ─────────────────────────
def _sb_headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}

def _sb_upsert(table, on_conflict, rows):
    if not rows: return
    for i in range(0, len(rows), 500):
        chunk = rows[i:i+500]
        r = requests.post(f"{SB_REST}/{table}", headers=_sb_headers(),
                          params={"on_conflict": on_conflict} if on_conflict else {},
                          json=chunk, timeout=60)
        if not r.ok: log.error("SB %s: %s", table, r.text[:200])

def _sb_select_pending(table, null_col, extra={}, select="*", limit=100):
    params = {null_col: "is.null", "select": select, "limit": str(limit)}
    params.update({k: f"eq.{v}" for k, v in extra.items()})
    r = requests.get(f"{SB_REST}/{table}", headers=_sb_headers(), params=params, timeout=30)
    return r.json() if r.ok else []

def _sb_delete(table, col, val):
    requests.delete(f"{SB_REST}/{table}", headers=_sb_headers(),
                    params={col: f"eq.{val}"}, timeout=30)

# ── Checkpoint ───────────────────────────────
def _load_checkpoint():
    try:
        if os.path.exists(CHECKPOINT_FILE):
            return json.loads(open(CHECKPOINT_FILE).read())
    except: pass
    return {}

def _save_checkpoint(data):
    try:
        json.dump(data, open(CHECKPOINT_FILE, "w"))
    except Exception as e:
        log.warning("No se pudo guardar checkpoint: %s", e)

# ── Parsers ──────────────────────────────────
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

def _str(v):
    if v is None: return None
    s = str(v).strip(); return s if s else None

def _float(v):
    try: return float(v) if v is not None else None
    except: return None

# ── 1. Backfill licitaciones históricas ──────
def backfill_licitaciones():
    log.info("── Backfill licitaciones históricas ──")
    cp      = _load_checkpoint()
    hoy     = date.today()
    min_dia = hoy - timedelta(days=DIAS_HISTORICO_LIC)

    last_str = cp.get("lic_fecha")
    if last_str:
        try:
            from datetime import date as dt_date
            last_dia = dt_date.fromisoformat(last_str) - timedelta(days=1)
        except: last_dia = hoy - timedelta(days=1)
    else:
        last_dia = hoy - timedelta(days=1)

    if last_dia < min_dia:
        log.info("Backfill licitaciones completo hasta %s", min_dia)
        return

    dia = last_dia
    total = 0

    while dia >= min_dia:
        fecha_str = dia.strftime("%d%m%Y")
        log.info("LIC fecha %s...", dia)
        try:
            data    = _get_v1({"fecha": fecha_str})
            time.sleep(SLEEP)
            listado = data.get("Listado") or []
            if not listado:
                dia -= timedelta(days=1); continue

            codigos = list({str(l.get("CodigoExterno") or "").strip()
                           for l in listado if str(l.get("CodigoExterno") or "").strip()})

            for codigo in codigos:
                try:
                    det     = _get_v1({"codigo": codigo})
                    time.sleep(SLEEP)
                    lics    = det.get("Listado") or []
                    if not lics: continue
                    lic     = lics[0]
                    fechas  = lic.get("Fechas") or {}
                    comp    = lic.get("Comprador") or {}
                    items   = ((lic.get("Items") or {}).get("Listado")) or []

                    cab = {
                        "codigo_externo":    codigo,
                        "nombre":            lic.get("Nombre"),
                        "tipo":              lic.get("Tipo"),
                        "estado":            lic.get("Estado"),
                        "codigo_estado":     lic.get("CodigoEstado"),
                        "moneda":            lic.get("Moneda"),
                        "fecha_publicacion": _ts(fechas.get("FechaPublicacion")),
                        "fecha_cierre":      _ts(fechas.get("FechaCierre")),
                        "fecha_adjudicacion":_ts(fechas.get("FechaAdjudicacion")),
                        "raw_hash":          _hash(lic),
                        "last_sync_at":      datetime.now(timezone.utc).isoformat(),
                    }
                    comp_row = {
                        "codigo_externo":   codigo,
                        "nombre_organismo": _str(comp.get("NombreOrganismo")),
                        "rut_unidad":       _str(comp.get("RutUnidad")),
                        "nombre_unidad":    _str(comp.get("NombreUnidad")),
                        "region_unidad":    _str(comp.get("RegionUnidad")),
                    }
                    _sb_upsert("mp_licitaciones",         "codigo_externo", [cab])
                    _sb_upsert("mp_licitacion_comprador", "codigo_externo", [comp_row])

                    # Items y adjudicaciones
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
                            "nombre_producto": _str(it.get("NombreProducto")),
                            "categoria":       _str(it.get("Categoria")),
                            "cantidad":        _num(it.get("Cantidad")),
                        })
                        adj = it.get("Adjudicacion")
                        for a in (adj if isinstance(adj, list) else ([adj] if isinstance(adj, dict) else [])):
                            if not isinstance(a, dict): continue
                            rut = _str(a.get("RutProveedor"))
                            if not rut: continue
                            key = f"{codigo}|{correl}|{rut}"
                            if key in seen_a: continue
                            seen_a.add(key)
                            adj_rows.append({
                                "licitacion_id":   codigo, "item_no": correl,
                                "proveedor_rut":   rut,
                                "proveedor_nombre":_str(a.get("NombreProveedor")),
                                "monto_total":     _num(a.get("MontoTotal")),
                            })
                    _sb_delete("mp_licitacion_items", "codigo_externo", codigo)
                    _sb_delete("mp_adjudicaciones",   "licitacion_id",  codigo)
                    if item_rows: _sb_upsert("mp_licitacion_items", "codigo_externo,correlativo", item_rows)
                    if adj_rows:  _sb_upsert("mp_adjudicaciones", "licitacion_id,item_no,proveedor_rut", adj_rows)
                    total += 1
                except Exception as e:
                    log.warning("Error lic %s: %s", codigo, repr(e))

        except Exception as e:
            log.error("Error fecha %s: %s", dia, repr(e))

        cp["lic_fecha"] = dia.isoformat()
        _save_checkpoint(cp)
        dia -= timedelta(days=1)
        time.sleep(1)

    log.info("Backfill licitaciones: %d procesadas", total)

# ── 2. Backfill Compra Ágil ──────────────────
def backfill_compra_agil():
    log.info("── Backfill Compra Ágil (sin detalle) ──")
    estados = ["cerrada","desierta","cancelada","proveedor_seleccionado"]
    pendientes = []
    for estado in estados:
        rows = _sb_select_pending(
            "mp_compra_agil", "detail_synced_at",
            extra={"estado_codigo": estado},
            select="id_mp", limit=BATCH_CA // len(estados) + 1
        )
        pendientes.extend([r["id_mp"] for r in rows if r.get("id_mp")])

    pendientes = pendientes[:BATCH_CA]
    log.info("CA pendientes: %d", len(pendientes))

    for id_mp in pendientes:
        try:
            det = _get_v2(f"/v2/compra-agil/{id_mp}", {})
            time.sleep(SLEEP)
            p  = det.get("presupuesto",{}); oc = det.get("orden_compra",{})
            extra = {
                "id_mp": id_mp,
                "descripcion":       det.get("descripcion"),
                "id_orden_compra":   oc.get("id_orden_compra"),
                "detail_synced_at":  datetime.now(timezone.utc).isoformat(),
            }
            _sb_upsert("mp_compra_agil", "id_mp", [extra])

            # Proveedores
            _sb_delete("mp_ca_proveedores_cotizando", id_mp)
            pv_rows = []
            for pv in det.get("proveedores_cotizando", []):
                pv_rows.append({
                    "id_mp": id_mp,
                    "rut_proveedor":          pv.get("rut_proveedor"),
                    "razon_social":           pv.get("razon_social"),
                    "id_cotizacion":          pv.get("id_cotizacion"),
                    "valor_neto":             _float(pv.get("valor_neto")),
                    "monto_total":            _float(pv.get("monto_total")),
                    "proveedor_seleccionado": (pv.get("seleccion") or {}).get("proveedor_seleccionado", False),
                })
            if pv_rows: _sb_upsert("mp_ca_proveedores_cotizando", "id_mp,id_cotizacion", pv_rows)
            log.info("CA detalle OK: %s", id_mp)
        except Exception as e:
            log.warning("Error CA %s: %s", id_mp, repr(e))

# ── 3. Backfill OC ───────────────────────────
def backfill_oc():
    log.info("── Backfill OC (sin total_neto) ──")
    pendientes = _sb_select_pending("mp_oc_header", "total_neto",
                                    select="codigo_oc", limit=BATCH_OC)
    codigos = [r["codigo_oc"] for r in pendientes if r.get("codigo_oc")]
    log.info("OC pendientes: %d", len(codigos))

    ESTADO_OC = {"4":"Enviada a Proveedor","5":"En proceso","6":"Aceptada",
                 "9":"Cancelada","12":"Recepción Conforme"}

    for codigo in codigos:
        try:
            data  = _session.get(MP_OC_API, params={"codigo": codigo, "ticket": MP_TICKET}, timeout=45)
            time.sleep(SLEEP)
            data.raise_for_status()
            lics = data.json().get("Listado") or []
            if not lics: continue
            oc   = lics[0]
            fechas = oc.get("Fechas") or {}
            comp   = oc.get("Comprador") or {}
            prov   = oc.get("Proveedor") or {}
            ec     = _str(oc.get("CodigoEstado"))
            hdr = {
                "codigo_oc":       codigo,
                "estado_codigo":   ec,
                "estado_texto":    ESTADO_OC.get(ec or "", _str(oc.get("Estado"))),
                "total_neto":      _float(oc.get("TotalNeto")),
                "total":           _float(oc.get("Total")),
                "fecha_envio":     _ts(fechas.get("FechaEnvio")),
                "comprador_nombre":_str(comp.get("NombreOrganismo")),
                "proveedor_nombre":_str(prov.get("Nombre")),
                "proveedor_rut":   _str(prov.get("RutSucursal")),
                "last_sync_at":    datetime.now(timezone.utc).isoformat(),
            }
            _sb_upsert("mp_oc_header", "codigo_oc", [hdr])
            log.info("OC OK: %s", codigo)
        except Exception as e:
            log.warning("Error OC %s: %s", codigo, repr(e))

# ── Main ─────────────────────────────────────
def main():
    log.info("=== backfill_noche === %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    backfill_licitaciones()
    backfill_compra_agil()
    backfill_oc()
    log.info("=== backfill_noche completado ===")

if __name__ == "__main__":
    main()
