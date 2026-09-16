"""
sync_compra_agil.py
===================
Busca Compras Ágiles nuevas o modificadas.
Programar: cada 2 h (workflow mp-compra-agil.yml, timeout 30 min).

Lógica:
  - Trae solo cambios desde la última ejecución (incremental)
  - La ventana [cursor → ahora] se recorre en TROZOS de CA_CHUNK_HORAS y el
    cursor se guarda al cerrar cada trozo, no al final: un fallo cuesta el trozo
    en curso y no la corrida entera, así el atraso siempre drena
  - Cursor en Supabase (mp_sync_cursor, key="compra_agil")
  - Para estados con detalle (cerrada+), trae proveedores/productos
  - Sale con código 1 si no cerró ni un trozo: un cursor clavado tiene que verse
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

# ── Ventanas troceadas ───────────────────────
# El cursor solo avanzaba si la ventana COMPLETA [cursor → ahora] se leía sin un
# solo error. Con el cursor atrasado esa ventana crece sin techo, y a mas paginas
# mas probable es que una falle: entonces no se avanza, la ventana crece otro
# tanto, y el atraso no se recupera nunca. (Paso: el cursor quedo clavado 9 dias
# y cada corrida repetia 236 paginas para terminar sin avanzar.)
#
# Ahora la ventana se trocea y el cursor se guarda al cerrar CADA trozo: un fallo
# cuesta el trozo en curso, no la corrida entera, y el atraso drena solo.
CHUNK_HORAS   = float(os.getenv("CA_CHUNK_HORAS", "6"))
# Presupuesto de pared para la fase de listado. El job tiene timeout de 30 min;
# se deja margen para la fase de detalles y para el arranque del runner.
PRESUP_LISTADO_S = float(os.getenv("CA_PRESUP_LISTADO_S", "1200"))   # 20 min
PRESUP_DETALLE_S = float(os.getenv("CA_PRESUP_DETALLE_S", "420"))    # 7 min
MAX_DETALLE      = int(os.getenv("CA_MAX_DETALLE", "300"))
# Reintentos hacia MP y hacia Supabase
MP_INTENTOS  = int(os.getenv("CA_MP_INTENTOS", "5"))
SB_INTENTOS  = int(os.getenv("CA_SB_INTENTOS", "4"))

ESTADOS_DETALLE = {"cerrada", "desierta", "cancelada", "proveedor_seleccionado"}

T_MAIN  = "mp_compra_agil"
T_PROVS = "mp_ca_proveedores_cotizando"
T_PRODS = "mp_ca_productos_solicitados"
T_DOCS  = "mp_ca_documentos"
T_PCOT  = "mp_ca_productos_cotizados"

# ── HTTP ─────────────────────────────────────
_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

class MPTransitorio(Exception):
    """429 o 5xx persistente de Mercado Público tras agotar los reintentos."""


def _mp_get(path: str, params: dict) -> dict:
    """GET a MP con backoff exponencial ante 429 y 5xx.

    Antes habia un solo reintento a los 60 s: en una ventana de cientos de
    paginas basta un 429 seguido de otro para tumbar la corrida completa, que es
    justo lo que impedia avanzar el cursor.
    """
    ultimo = None
    for intento in range(MP_INTENTOS):
        try:
            r = _session.get(f"{MP_BASE}{path}",
                             headers={"ticket": MP_TICKET}, params=params, timeout=45)
        except requests.RequestException as e:
            ultimo = e
        else:
            if r.status_code == 429 or r.status_code >= 500:
                ultimo = f"HTTP {r.status_code}"
            else:
                r.raise_for_status()
                data = r.json()
                if data.get("success") != "OK":
                    # Error de negocio: reintentar no ayuda.
                    raise RuntimeError(f"API error: {data.get('errors')}")
                return data["payload"]

        if intento < MP_INTENTOS - 1:
            espera = min(120.0, (2 ** intento) * 15) + random.uniform(0, 5)
            log.warning("MP %s (%s) — reintento %d/%d en %.0fs",
                        path, ultimo, intento + 1, MP_INTENTOS - 1, espera)
            time.sleep(espera)

    raise MPTransitorio(f"{path}: {ultimo} tras {MP_INTENTOS} intentos")

def _sb_headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}

def _sb_request(metodo, table, **kw):
    """Llamada a Supabase con reintentos ante 429/5xx; levanta si no se logra.

    Una escritura que solo se loguea deja al cursor avanzar sobre datos que nunca
    entraron: la ventana no se vuelve a consultar y el hueco es permanente.
    """
    ultimo = None
    for intento in range(SB_INTENTOS):
        try:
            r = requests.request(metodo, f"{SB_REST}/{table}",
                                 headers=_sb_headers(), timeout=60, **kw)
        except requests.RequestException as e:
            ultimo = repr(e)
        else:
            if r.ok:
                return r
            ultimo = f"{r.status_code}: {r.text[:200]}"
            if 400 <= r.status_code < 500 and r.status_code != 429:
                # Payload o esquema mal: reintentar no cambia nada.
                raise RuntimeError(f"SB {metodo} {table} — {ultimo}")

        if intento < SB_INTENTOS - 1:
            espera = min(60.0, (2 ** intento) * 5) + random.uniform(0, 3)
            log.warning("SB %s %s (%s) — reintento %d/%d en %.0fs",
                        metodo, table, ultimo, intento + 1, SB_INTENTOS - 1, espera)
            time.sleep(espera)

    raise RuntimeError(f"SB {metodo} {table} falló tras {SB_INTENTOS} intentos — {ultimo}")

def _sb_upsert(table, on_conflict, rows):
    if not rows: return
    _sb_request("POST", table,
                params={"on_conflict": on_conflict} if on_conflict else {},
                json=rows)

def _sb_delete(table, id_mp):
    _sb_request("DELETE", table, params={"id_mp": f"eq.{id_mp}"})

def _sb_select(table, id_mp):
    return _sb_request("GET", table,
                       params={"select": "*", "id_mp": f"eq.{id_mp}"}).json()

def _sb_con_detalle(ids: list) -> set:
    """IDs que ya tienen detalle sincronizado, en bloque.

    Reemplaza un SELECT por item: con un atraso de miles de compras ágiles, esa
    consulta uno-a-uno era la mitad del costo de la fase de detalles.
    """
    if not ids: return set()
    out = set()
    for i in range(0, len(ids), 100):
        lote = ids[i:i + 100]
        filas = _sb_request("GET", T_MAIN, params={
            "select": "id_mp",
            "id_mp": f"in.({','.join(lote)})",
            "detail_synced_at": "not.is.null",
            "id_orden_compra": "not.is.null",
            "limit": str(len(lote) + 1),
        }).json()
        out.update(f["id_mp"] for f in filas)
    return out

# ── Cursor (persistido en Supabase: mp_sync_cursor, key="compra_agil") ──
def _fmt(dt: datetime) -> str:
    """Formato que espera la API de compra ágil: hora Chile, sin zona."""
    return dt.astimezone(TZ_CL).strftime("%Y-%m-%dT%H:%M:%S")

def _load_cursor() -> datetime:
    """Momento desde el cual hay que leer, como datetime con zona Chile."""
    try:
        data = load_cursor("compra_agil", {})
        crudo = (data or {}).get("ultimo_cambio")
        if crudo:
            dt = datetime.fromisoformat(crudo.replace("Z", "+00:00"))
            # Los cursores viejos se guardaron sin zona, en hora Chile.
            return dt.astimezone(TZ_CL) if dt.tzinfo else dt.replace(tzinfo=TZ_CL)
    except Exception as e:
        log.warning("Cursor ilegible (%r) — se usa la ventana por defecto", e)
    return datetime.now(TZ_CL) - timedelta(hours=VENTANA_HORAS)

def _save_cursor(dt: datetime):
    save_cursor("compra_agil", {"ultimo_cambio": _fmt(dt)})

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
            # El flag viene al nivel superior del proveedor (no bajo "seleccion")
            "proveedor_seleccionado": bool(pv.get("proveedor_seleccionado", 0)),
            "motivo_seleccion":       pv.get("estado_por_comprador") or pv.get("justificacion_inadmisibilidad"),
        })
    if pv_rows:
        _sb_upsert(T_PROVS, "id_mp,id_cotizacion", pv_rows)

# ── Main ─────────────────────────────────────
def _procesar_ventana(desde: datetime, hasta: datetime) -> tuple:
    """Lee y escribe un trozo completo de ventana. Levanta si no lo logra entero.

    Devuelve (filas_escritas, ids_con_detalle_pendiente). El trozo es la unidad
    atómica de progreso: solo si vuelve sin excepción el cursor puede avanzar.
    """
    params = {
        "cambio_desde":  _fmt(desde),
        "cambio_hasta":  _fmt(hasta),
        "ordenar_por":   "FechaUltimaModificacion",
        "tamano_pagina": PAGE_SIZE,
        "numero_pagina": 1,
    }

    filas, pendientes, pagina, total_paginas = 0, [], 1, "?"
    while True:
        params["numero_pagina"] = pagina
        payload = _mp_get("/v2/compra-agil", params)
        time.sleep(SLEEP)

        pag   = payload["paginacion"]
        items = payload["items"]
        total_paginas = pag["total_paginas"]
        if not items: break

        _sb_upsert(T_MAIN, "id_mp", [_parse_main(i) for i in items])
        filas += len(items)
        pendientes += [i["codigo"] for i in items
                       if (i.get("estado") or {}).get("codigo") in ESTADOS_DETALLE]

        if pagina >= total_paginas: break
        pagina += 1

    log.info("  %s → %s: %d filas en %s pág.",
             _fmt(desde)[5:16], _fmt(hasta)[5:16], filas, total_paginas)
    return filas, pendientes


def main():
    t0       = time.monotonic()
    ahora_cl = datetime.now(TZ_CL)
    desde    = _load_cursor()
    atraso_h = (ahora_cl - desde).total_seconds() / 3600

    log.info("=== sync_compra_agil === desde %s (atraso %.1f h, trozos de %.0f h)",
             _fmt(desde), atraso_h, CHUNK_HORAS)
    if atraso_h > 24:
        log.warning("Atraso de %.1f días: la corrida drena lo que alcance y "
                    "el resto queda para la siguiente.", atraso_h / 24)

    total, pendientes, trozos = 0, [], 0
    corte = None

    while desde < ahora_cl:
        if time.monotonic() - t0 > PRESUP_LISTADO_S:
            corte = "presupuesto de tiempo"
            break

        hasta = min(desde + timedelta(hours=CHUNK_HORAS), ahora_cl)
        try:
            filas, pend = _procesar_ventana(desde, hasta)
        except Exception as e:
            # El trozo en curso se pierde; los ya cerrados quedan guardados.
            corte = repr(e)
            log.error("Trozo %s → %s falló: %s", _fmt(desde), _fmt(hasta), repr(e))
            break

        # Solo aquí, con el trozo íntegramente leído Y escrito, avanza el cursor.
        _save_cursor(hasta)
        desde   = hasta
        total  += filas
        trozos += 1
        pendientes += pend

    log.info("Listado: %d trozos cerrados, %d filas. Cursor en %s (atraso %.1f h)",
             trozos, total, _fmt(desde), (ahora_cl - desde).total_seconds() / 3600)
    if corte:
        log.error("Listado incompleto (%s) — se retoma desde el cursor en la próxima corrida.", corte)

    # ── Detalles: enriquecimiento idempotente, no bloquea el cursor ──
    pendientes = list(dict.fromkeys(pendientes))          # únicos, orden estable
    errores_detalle = 0
    if pendientes:
        ya = _sb_con_detalle(pendientes)
        faltan = [i for i in pendientes if i not in ya][:MAX_DETALLE]
        log.info("Detalles: %d candidatos, %d ya tenían, %d en esta corrida",
                 len(pendientes), len(ya), len(faltan))
        for id_mp in faltan:
            if time.monotonic() - t0 > PRESUP_LISTADO_S + PRESUP_DETALLE_S:
                log.warning("Presupuesto de detalles agotado — quedan %d para la próxima",
                            len(faltan) - faltan.index(id_mp))
                break
            try:
                _sync_detalle(id_mp)
            except Exception as e:
                errores_detalle += 1
                log.warning("Error detalle %s: %s", id_mp, repr(e))

    # El job tiene que ponerse rojo si el listado no avanzó: un cursor clavado es
    # exactamente el modo de falla que dejó 9 días sin sincronizar en silencio.
    if trozos == 0 and corte:
        log.error("Ningún trozo cerrado en esta corrida.")
        sys.exit(1)
    if errores_detalle:
        log.warning("%d detalles con error (se reintentan solos la próxima corrida)",
                    errores_detalle)

if __name__ == "__main__":
    main()
