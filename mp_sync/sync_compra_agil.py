"""
sync_compra_agil.py
===================
Busca Compras Ágiles nuevas o modificadas.
Programar: cada 30 minutos.

Lógica:
  - Trae solo cambios desde la última ejecución (incremental)
  - Guarda timestamp en .cursor_ca.json
  - Para estados con detalle (cerrada+), trae proveedores/productos
"""

import os, sys, time, json, logging, requests, random
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

TZ_CL = ZoneInfo("America/Santiago")

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

from cursor_store import load_cursor, save_cursor

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("sync_ca")

# ── Config ──────────────────────────────────
MP_TICKET    = os.environ["TICKET_CA"]
MP_BASE      = "https://api2.mercadopublico.cl"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"

SLEEP        = float(os.getenv("SLEEP_BETWEEN", "2.0"))
PAGE_SIZE    = 50
CURSOR_FILE  = os.getenv("CA_CURSOR_FILE",
               os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cursor_ca.json"))
VENTANA_HORAS = int(os.getenv("CA_VENTANA_HORAS", "1"))  # horas atrás si no hay cursor

ESTADOS_DETALLE = {"cerrada", "desierta", "cancelada", "proveedor_seleccionado"}

T_MAIN  = "mp_compra_agil"
T_PROVS = "mp_ca_proveedores_cotizando"
T_PRODS = "mp_ca_productos_solicitados"
T_DOCS  = "mp_ca_documentos"
T_PCOT  = "mp_ca_productos_cotizados"

# ── HTTP ─────────────────────────────────────
_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

def _mp_get(path: str, params: dict) -> dict:
    r = _session.get(f"{MP_BASE}{path}",
                     headers={"ticket": MP_TICKET}, params=params, timeout=45)
    if r.status_code == 429:
        log.warning("429 — esperando 60s...")
        time.sleep(60)
        r = _session.get(f"{MP_BASE}{path}",
                         headers={"ticket": MP_TICKET}, params=params, timeout=45)
    r.raise_for_status()
    data = r.json()
    if data.get("success") != "OK":
        raise RuntimeError(f"API error: {data.get('errors')}")
    return data["payload"]

def _sb_headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}

def _sb_upsert(table, on_conflict, rows):
    if not rows: return
    r = requests.post(f"{SB_REST}/{table}", headers=_sb_headers(),
                      params={"on_conflict": on_conflict} if on_conflict else {},
                      json=rows, timeout=60)
    if not r.ok: log.error("SB %s: %s", table, r.text[:200])

def _sb_delete(table, id_mp):
    requests.delete(f"{SB_REST}/{table}", headers=_sb_headers(),
                    params={"id_mp": f"eq.{id_mp}"}, timeout=30)

def _sb_select(table, id_mp):
    r = requests.get(f"{SB_REST}/{table}", headers=_sb_headers(),
                     params={"select": "*", "id_mp": f"eq.{id_mp}"}, timeout=30)
    return r.json() if r.ok else []

# ── Cursor (persistido en Supabase: mp_sync_cursor, key="compra_agil") ──
def _load_cursor() -> str:
    try:
        data = load_cursor("compra_agil", {})
        if data.get("ultimo_cambio"):
            return data["ultimo_cambio"]
    except: pass
    # Sin cursor: última ventana en hora Chile
    desde = datetime.now(TZ_CL) - timedelta(hours=VENTANA_HORAS)
    return desde.strftime("%Y-%m-%dT%H:%M:%S")

def _save_cursor(ts: str):
    save_cursor("compra_agil", {"ultimo_cambio": ts})

# ── Parsers ──────────────────────────────────
def _date(v): return v[:10] if v else None
def _ts(v):   return v if v else None
def _float(v):
    try: return float(v) if v is not None else None
    except: return None

def _parse_main(item: dict) -> dict:
    f, m, i = item.get("fechas",{}), item.get("montos",{}), item.get("institucion",{})
    e, c, r  = item.get("estado",{}), item.get("convocatoria",{}), item.get("resumen",{})
    mo       = item.get("motivos", {})
    return {
        "id_mp":                   item.get("codigo",""),
        "nombre":                  item.get("nombre","").strip(),
        "estado_codigo":           e.get("codigo"),
        "estado_glosa":            e.get("glosa"),
        "estado_convocatoria":     c.get("estado_convocatoria"),
        "fecha_publicacion":       _date(f.get("fecha_publicacion")),
        "fecha_cierre":            _date(f.get("fecha_cierre")),
        "fecha_cancelacion":       _date(f.get("fecha_cancelacion")),
        "fecha_ultimo_cambio":     _ts(f.get("fecha_ultimo_cambio")),
        "organismo":               i.get("organismo_comprador","").strip(),
        "rut_organismo":           i.get("rut"),
        "unidad":                  i.get("unidad_compra","").strip(),
        "region":                  i.get("region"),
        "nombre_region":           i.get("nombre_region"),
        "monto_disponible":        _float(m.get("monto_disponible")),
        "moneda":                  m.get("moneda","CLP"),
        "monto_disponible_clp":    _float(m.get("monto_disponible_clp")),
        "total_ofertas":           r.get("total_ofertas_recibidas", 0),
        "motivo_cancelacion":      mo.get("motivo_cancelacion"),
        "synced_at":               datetime.now(timezone.utc).isoformat(),
    }

def _sync_detalle(id_mp: str):
    det = _mp_get(f"/v2/compra-agil/{id_mp}", {})
    time.sleep(SLEEP)
    p = det.get("presupuesto",{}); oc = det.get("orden_compra",{})
    ent = det.get("entrega",{}); fl = det.get("flags",{})
    extra = {
        "id_mp": id_mp,
        "descripcion":          det.get("descripcion"),
        "tipo_presupuesto":     p.get("tipo_presupuesto"),
        "id_orden_compra":      oc.get("id_orden_compra"),
        "id_oc":                oc.get("id_oc"),
        "direccion_entrega":    ent.get("direccion_entrega"),
        "plazo_entrega_dias":   ent.get("plazo_entrega_dias"),
        "considera_medioambiental": fl.get("considera_requisitos_medioambientales", False),
        "detail_synced_at":     datetime.now(timezone.utc).isoformat(),
    }
    _sb_upsert(T_MAIN, "id_mp", [extra])

    # Docs
    _sb_delete(T_DOCS, id_mp)
    docs = [{"id_mp": id_mp, "doc_uuid": d.get("id"), "nombre": d.get("nombre")}
            for d in det.get("documentos", [])]
    if docs: _sb_upsert(T_DOCS, "id_mp,doc_uuid", docs)

    # Productos solicitados
    _sb_delete(T_PRODS, id_mp)
    prods = [{"id_mp": id_mp, "codigo_producto": str(p.get("codigo_producto","")),
              "nombre": p.get("nombre"), "cantidad": _float(p.get("cantidad")),
              "unidad_medida": p.get("unidad_medida")}
             for p in det.get("productos_solicitados", [])]
    if prods: _sb_upsert(T_PRODS, "", prods)

    # Proveedores
    _sb_delete(T_PCOT, id_mp)
    _sb_delete(T_PROVS, id_mp)
    pv_rows = []
    for pv in det.get("proveedores_cotizando", []):
        pv_rows.append({
            "id_mp": id_mp,
            "rut_proveedor":          pv.get("rut_proveedor"),
            "razon_social":           pv.get("razon_social"),
            "es_emt":                 pv.get("es_emt", False),
            "id_cotizacion":          pv.get("id_cotizacion"),
            "valor_neto":             _float(pv.get("valor_neto")),
            "monto_total":            _float(pv.get("monto_total")),
            "proveedor_seleccionado": (pv.get("seleccion") or {}).get("proveedor_seleccionado", False),
            "motivo_seleccion":       (pv.get("seleccion") or {}).get("motivo_seleccion"),
        })
    if pv_rows:
        _sb_upsert(T_PROVS, "id_mp,id_cotizacion", pv_rows)

# ── Main ─────────────────────────────────────
def main():
    ahora    = datetime.now(timezone.utc)
    ahora_cl = ahora.astimezone(TZ_CL)
    desde    = _load_cursor()

    # La API de compra ágil trabaja en hora Chile — convertir si viene en UTC
    if desde.endswith("Z"):
        try:
            dt_desde = datetime.fromisoformat(desde.replace("Z", "+00:00"))
            desde = dt_desde.astimezone(TZ_CL).strftime("%Y-%m-%dT%H:%M:%S")
        except: pass

    log.info("=== sync_compra_agil === desde: %s (hora Chile)", desde)

    params = {
        "cambio_desde":  desde,
        "cambio_hasta":  ahora_cl.strftime("%Y-%m-%dT%H:%M:%S"),
        "ordenar_por":   "FechaUltimaModificacion",
        "tamano_pagina": PAGE_SIZE,
        "numero_pagina": 1,
    }

    total = 0
    pendientes_detalle = []
    pagina = 1

    while True:
        params["numero_pagina"] = pagina
        try:
            payload = _mp_get("/v2/compra-agil", params)
            time.sleep(SLEEP)
        except Exception as e:
            log.error("Error listado página %d: %s", pagina, repr(e))
            break

        pag   = payload["paginacion"]
        items = payload["items"]
        if not items: break

        rows = [_parse_main(i) for i in items]
        _sb_upsert(T_MAIN, "id_mp", rows)
        total += len(rows)

        for item in items:
            if (item.get("estado") or {}).get("codigo") in ESTADOS_DETALLE:
                pendientes_detalle.append(item["codigo"])

        log.info("Página %d/%d → %d filas", pagina, pag["total_paginas"], len(rows))
        if pagina >= pag["total_paginas"]: break
        pagina += 1

    log.info("Listado: %d filas. Detalles pendientes: %d", total, len(pendientes_detalle))

    # Guardar el cursor APENAS termina el listado. La fase de detalles es
    # enriquecimiento (idempotente) y no debe bloquear el avance del cursor:
    # si falla o es lenta, no queremos reprocesar todo el listado en la próxima
    # corrida (eso causaba runs gigantes de miles de filas).
    _save_cursor(ahora_cl.strftime("%Y-%m-%dT%H:%M:%S"))
    log.info("Cursor guardado: %s (hora Chile)", ahora_cl.strftime("%Y-%m-%dT%H:%M:%S"))

    for id_mp in pendientes_detalle:
        try:
            rows = _sb_select(T_MAIN, id_mp)
            if rows and rows[0].get("detail_synced_at") and rows[0].get("id_orden_compra"):
                continue
            _sync_detalle(id_mp)
        except Exception as e:
            log.warning("Error detalle %s: %s", id_mp, repr(e))

if __name__ == "__main__":
    main()
