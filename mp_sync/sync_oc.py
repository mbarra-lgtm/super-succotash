"""
sync_oc.py
==========
Sincroniza Órdenes de Compra del día.
Programar: cada 30 minutos.

IMPORTANTE: el listado por fecha SOLO trae {Codigo, Nombre, CodigoEstado}.
Para obtener Fechas, Comprador, Proveedor, Items, montos y CodigoLicitacion
hay que pedir el DETALLE por código (?codigo=XXX). Este script:
  1. Lista los códigos del día (estado=todos).
  2. Compara contra BD: pide detalle solo de OC nuevas o que cambiaron de estado.
  3. Parsea el detalle completo al esquema rico de mp_oc_header / mp_oc_items.
"""

import os, time, json, hashlib, logging, requests
from datetime import datetime, timezone, timedelta, date

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("sync_oc")

MP_TICKET    = os.environ["TICKET_OC"]
MP_OC_API    = "https://api.mercadopublico.cl/servicios/v1/publico/ordenesdecompra.json"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"
SLEEP        = float(os.getenv("SLEEP_BETWEEN_OC", "2.0"))
OC_DIAS      = int(os.getenv("OC_DIAS_ATRAS", "1"))           # cuántos días atrás listar
OC_MAX_DET   = int(os.getenv("OC_MAX_DETALLE", "400"))        # tope de detalles por ejecución

T_HDR  = "mp_oc_header"
T_ITEMS= "mp_oc_items"

ESTADO_OC = {
    "4":"Enviada a Proveedor","5":"En proceso","6":"Aceptada",
    "9":"Cancelada","12":"Recepción Conforme","13":"Pendiente de Recepcionar",
    "14":"Recepcionada Parcialmente","15":"Recepción Conforme Incompleta",
}

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

def _mp_get(params):
    r = _session.get(MP_OC_API, params={**params, "ticket": MP_TICKET}, timeout=45)
    if r.status_code == 429:
        log.warning("429 — esperando 60s...")
        time.sleep(60)
        r = _session.get(MP_OC_API, params={**params, "ticket": MP_TICKET}, timeout=45)
    r.raise_for_status()
    return r.json()

def _sb_headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}

def _sb_upsert(table, on_conflict, rows):
    if not rows: return 0
    r = requests.post(f"{SB_REST}/{table}", headers=_sb_headers(),
                      params={"on_conflict": on_conflict} if on_conflict else {},
                      json=rows, timeout=60)
    if not r.ok: log.error("SB %s: %s", table, r.text[:300])
    return len(rows)

def _sb_delete(table, col, val):
    requests.delete(f"{SB_REST}/{table}", headers=_sb_headers(),
                    params={col: f"eq.{val}"}, timeout=30)

def _sb_estados_bulk(codigos):
    """Retorna dict codigo_oc -> (estado_codigo, tiene_raw) de las ya en BD."""
    out = {}
    for i in range(0, len(codigos), 100):
        chunk = codigos[i:i+100]
        r = requests.get(f"{SB_REST}/{T_HDR}", headers=_sb_headers(),
                         params={"select": "codigo_oc,estado_codigo,raw_hash",
                                 "codigo_oc": f"in.({','.join(chunk)})",
                                 "limit": str(len(chunk)+1)}, timeout=30)
        if r.ok:
            for row in r.json():
                out[row["codigo_oc"]] = (row.get("estado_codigo"), bool(row.get("raw_hash")))
    return out

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

def _float(v):
    try: return float(str(v).replace(",",".").strip()) if v is not None else None
    except: return None

def _str(v):
    if v is None: return None
    s = str(v).strip()
    return s if s else None

def parse_oc_detalle(oc: dict):
    """Parsea el DETALLE completo de una OC al esquema mp_oc_header + mp_oc_items."""
    codigo = _str(oc.get("Codigo"))
    fechas = oc.get("Fechas") or {}
    comp   = oc.get("Comprador") or {}
    prov   = oc.get("Proveedor") or {}
    items  = ((oc.get("Items") or {}).get("Listado")) or []
    ec     = _str(oc.get("CodigoEstado"))

    hdr = {
        "codigo_oc":            codigo,
        "codigo_licitacion":    _str(oc.get("CodigoLicitacion")),
        "nombre_oc":            _str(oc.get("Nombre")),
        "descripcion":          _str(oc.get("Descripcion")),
        "tipo_oc":              _str(oc.get("Tipo")),
        "codigo_tipo":          _str(oc.get("CodigoTipo")),
        "moneda":               _str(oc.get("TipoMoneda")),
        "estado_codigo":        ec,
        "estado_texto":         _str(oc.get("Estado")) or ESTADO_OC.get(ec or "", None),
        "codigo_estado_proveedor": _str(oc.get("CodigoEstadoProveedor")),
        "estado_proveedor":     _str(oc.get("EstadoProveedor")),
        "tiene_items":          bool(oc.get("TieneItems")),
        "promedio_calificacion":_float(oc.get("PromedioCalificacion")),
        "cantidad_evaluacion":  oc.get("CantidadEvaluacion"),
        "descuentos":           _float(oc.get("Descuentos")),
        "cargos":               _float(oc.get("Cargos")),
        "total_neto":           _float(oc.get("TotalNeto")),
        "porcentaje_iva":       _float(oc.get("PorcentajeIva")),
        "total_impuestos":      _float(oc.get("Impuestos")),
        "total":                _float(oc.get("Total")),
        "financiamiento":       _str(oc.get("Financiamiento")),
        "pais":                 _str(oc.get("Pais")),
        "tipo_despacho":        _str(oc.get("TipoDespacho")),
        "forma_pago":           _str(oc.get("FormaPago")),
        "fecha_creacion":       _ts(fechas.get("FechaCreacion")),
        "fecha_envio":          _ts(fechas.get("FechaEnvio")),
        "fecha_aceptacion":     _ts(fechas.get("FechaAceptacion")),
        "fecha_cancelacion":    _ts(fechas.get("FechaCancelacion")),
        "fecha_ultima_modificacion": _ts(fechas.get("FechaUltimaModificacion")),
        "comprador_codigo_organismo": _str(comp.get("CodigoOrganismo")),
        "comprador_nombre":     _str(comp.get("NombreOrganismo")),
        "comprador_rut":        _str(comp.get("RutUnidad")),
        "comprador_codigo_unidad": _str(comp.get("CodigoUnidad")),
        "comprador_unidad":     _str(comp.get("NombreUnidad")),
        "comprador_actividad":  _str(comp.get("Actividad")),
        "comprador_comuna":     _str(comp.get("ComunaUnidad")),
        "comprador_region":     _str(comp.get("RegionUnidad")),
        "comprador_pais":       _str(comp.get("Pais")),
        "comprador_nombre_contacto": _str(comp.get("NombreContacto")),
        "comprador_cargo_contacto":  _str(comp.get("CargoContacto")),
        "comprador_fono_contacto":   _str(comp.get("FonoContacto")),
        "comprador_mail_contacto":   _str(comp.get("MailContacto")),
        "proveedor_codigo":     _str(prov.get("Codigo")),
        "proveedor_nombre":     _str(prov.get("Nombre")),
        "proveedor_actividad":  _str(prov.get("Actividad")),
        "proveedor_codigo_sucursal": _str(prov.get("CodigoSucursal")),
        "proveedor_nombre_sucursal": _str(prov.get("NombreSucursal")),
        "proveedor_rut":        _str(prov.get("RutSucursal")),
        "proveedor_direccion":  _str(prov.get("Direccion")),
        "proveedor_comuna":     _str(prov.get("Comuna")),
        "proveedor_region":     _str(prov.get("Region")),
        "proveedor_pais":       _str(prov.get("Pais")),
        "proveedor_nombre_contacto": _str(prov.get("NombreContacto")),
        "proveedor_cargo_contacto":  _str(prov.get("CargoContacto")),
        "proveedor_fono_contacto":   _str(prov.get("FonoContacto")),
        "proveedor_mail_contacto":   _str(prov.get("MailContacto")),
        "source":               "sync_oc",
        "raw":                  oc,
        "raw_hash":             _hash(oc),
        "last_sync_at":         datetime.now(timezone.utc).isoformat(),
    }

    item_rows = []
    for it in items:
        if not isinstance(it, dict): continue
        try: ln = int(it.get("Correlativo"))
        except: continue
        row = {
            "codigo_oc":        codigo,
            "line_no":          ln,
            "producto_codigo":  _str(it.get("CodigoProducto")),
            "producto_nombre":  _str(it.get("Producto")),
            "codigo_categoria": _str(it.get("CodigoCategoria")),
            "categoria":        _str(it.get("Categoria")),
            "especificacion_comprador": _str(it.get("EspecificacionComprador")),
            "especificacion_proveedor": _str(it.get("EspecificacionProveedor")),
            "unidad":           _str(it.get("Unidad")),
            "cantidad":         _float(it.get("Cantidad")),
            "precio_unitario":  _float(it.get("PrecioNeto")),
            "total_linea":      _float(it.get("Total")),
            "impuestos_linea":  _float(it.get("TotalImpuestos")),
            "total_cargos":     _float(it.get("TotalCargos")),
            "total_descuentos": _float(it.get("TotalDescuentos")),
            "raw":              it,
        }
        row["line_hash"] = _hash(row)
        item_rows.append(row)
    return hdr, item_rows

def _fetch_detalle(codigo):
    data = _mp_get({"codigo": codigo})
    lics = data.get("Listado") or []
    return lics[0] if lics else None

def main():
    log.info("=== sync_oc ===")
    ahora = date.today()

    # 1. Listar códigos de los días pedidos
    listados = {}  # codigo -> estado_codigo (del listado)
    for dias in range(OC_DIAS):
        dia = ahora - timedelta(days=dias)
        fecha_str = dia.strftime("%d%m%Y")
        try:
            data = _mp_get({"fecha": fecha_str, "estado": "todos"})
            time.sleep(SLEEP)
            listado = data.get("Listado") or []
        except Exception as e:
            log.error("Error listado %s: %s", dia, repr(e))
            continue
        for oc in listado:
            cod = _str(oc.get("Codigo"))
            if cod: listados[cod] = _str(oc.get("CodigoEstado"))
        log.info("%s → %d OC en listado", dia, len(listado))

    if not listados:
        log.info("Sin OC en el rango.")
        return

    # 2. Estado actual en BD → decidir cuáles necesitan detalle
    codigos = list(listados.keys())
    en_bd   = _sb_estados_bulk(codigos)
    pendientes = []
    for cod, est_lista in listados.items():
        prev = en_bd.get(cod)
        if prev is None:                       # nueva
            pendientes.append(cod)
        else:
            prev_estado, tiene_raw = prev
            if (not tiene_raw) or (str(prev_estado) != str(est_lista)):  # sin detalle o cambió estado
                pendientes.append(cod)

    log.info("Listado: %d | en BD: %d | necesitan detalle: %d (tope %d)",
             len(codigos), len(en_bd), len(pendientes), OC_MAX_DET)

    # 3. Traer detalle (con tope por ejecución) y upsert completo
    ok = err = sin_det = 0
    for cod in pendientes[:OC_MAX_DET]:
        try:
            oc = _fetch_detalle(cod)
            time.sleep(SLEEP)
            if not oc:
                sin_det += 1
                continue
            hdr, items = parse_oc_detalle(oc)
            _sb_upsert(T_HDR, "codigo_oc", [hdr])
            if items:
                _sb_delete(T_ITEMS, "codigo_oc", cod)
                _sb_upsert(T_ITEMS, "codigo_oc,line_no", items)
            ok += 1
        except Exception as e:
            log.warning("Error detalle %s: %s", cod, repr(e))
            err += 1

    restantes = max(0, len(pendientes) - OC_MAX_DET)
    log.info("Resultado: %d OC con detalle, %d sin detalle, %d errores | %d quedan para la próxima",
             ok, sin_det, err, restantes)

if __name__ == "__main__":
    main()
