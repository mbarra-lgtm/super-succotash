"""
ingesta_oc_datos_abiertos.py
============================
Carga masiva de Órdenes de Compra desde Datos Abiertos de ChileCompra
(https://datos-abiertos.chilecompra.cl) — SIN usar la API transaccional.

Archivos mensuales (blob Azure público, ~90 MB zip, CSV ';' una fila por ítem):
  https://transparenciachc.blob.core.windows.net/oc-da/{AÑO}-{MES}.zip  (mes sin cero)

Estrategia acordada:
  - HEADERS: se cargan TODAS las OCs del mes (match OC↔licitación de todo el mercado).
  - ITEMS: solo de OCs de RUTs objetivo (grupo+competidores) o con CodigoLicitacion.
  - raw_hash se marca 'da:{mes}' para que los backfills por API las salten.

Config por env:
  OC_DA_MESES    p.ej. "2025-12,2026-1,2026-2"  (default: dic-2025 → may-2026)
  OC_DA_LOCALZIP ruta a un zip ya descargado (para pruebas; ignora OC_DA_MESES)
  OC_DA_LIMIT    tope de filas a procesar (smoke test)
"""

import os, csv, io, sys, time, zipfile, tempfile, logging, requests
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("oc_da")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"
T_HDR, T_ITEMS = "mp_oc_header", "mp_oc_items"

BLOB = "https://transparenciachc.blob.core.windows.net/oc-da/{mes}.zip"
MESES_DEFAULT = "2025-12,2026-1,2026-2,2026-3,2026-4,2026-5"
MESES  = [m.strip() for m in (os.getenv("OC_DA_MESES") or MESES_DEFAULT).split(",") if m.strip()]
LOCALZIP = os.getenv("OC_DA_LOCALZIP") or None
LIMIT    = int(os.getenv("OC_DA_LIMIT", "0"))  # 0 = sin tope

RUTS_TARGET = {
    "87.927.900-3", "77.712.689-K", "76.708.952-K",           # grupo
    "96.877.150-7", "76.410.092-1", "76.092.123-8",           # Peña Spoerer, Dikar, L.J.
    "77.428.081-2", "92.475.000-6",                           # Mototech, Kaufmann
}
RUTS_NORM = {r.replace(".", "").replace("-", "").upper() for r in RUTS_TARGET}

def _norm_rut(v):
    return (v or "").replace(".", "").replace("-", "").strip().upper()

def _num(v):
    s = (v or "").strip()
    if not s: return None
    try: return float(s.replace(".", "").replace(",", ".")) if ("," in s) else float(s)
    except: return None

import re
_RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}")

def _dt(v):
    s = (v or "").strip()
    return s if _RE_FECHA.match(s) else None   # descarta "NA", "0000-00-00", vacíos

def _s(v):
    s = (v or "").strip()
    return s or None

def _sb_headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}

def _sb_upsert(table, on_conflict, rows, retries=3):
    if not rows: return
    for i in range(retries):
        r = requests.post(f"{SB_REST}/{table}", headers=_sb_headers(),
                          params={"on_conflict": on_conflict}, json=rows, timeout=120)
        if r.ok: return
        log.warning("SB %s intento %d: %s", table, i + 1, r.text[:200])
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"upsert {table} falló tras {retries} intentos")

def parse_header(r, mes, now_iso):
    return {
        "codigo_oc":          _s(r.get("Codigo")),
        "codigo_licitacion":  _s(r.get("CodigoLicitacion")),
        "nombre_oc":          _s(r.get("Nombre")),
        "descripcion":        _s(r.get("Descripcion/Obervaciones")),
        "tipo_oc":            _s(r.get("Tipo")),
        "codigo_tipo":        _s(r.get("CodigoTipo")),
        "tipo_oc_descripcion": _s(r.get("DescripcionTipoOC")),
        "moneda":             _s(r.get("TipoMonedaOC")),
        "estado_codigo":      _s(r.get("codigoEstado")),
        "estado_texto":       _s(r.get("Estado")),
        "codigo_estado_proveedor": _s(r.get("codigoEstadoProveedor")),
        "estado_proveedor":   _s(r.get("EstadoProveedor")),
        "tiene_items":        (r.get("tieneItems") or "").strip() == "1",
        "promedio_calificacion": _num(r.get("PromedioCalificacion")),
        "descuentos":         _num(r.get("Descuentos")),
        "cargos":             _num(r.get("Cargos")),
        "total_neto":         _num(r.get("TotalNetoOC")),
        "total_impuestos":    _num(r.get("Impuestos")),
        "total":              _num(r.get("MontoTotalOC_PesosChilenos")) or _num(r.get("MontoTotalOC")),
        "porcentaje_iva":     _num(r.get("PorcentajeIva")),
        "financiamiento":     _s(r.get("Financiamiento")),
        "pais":               _s(r.get("Pais")),
        "tipo_despacho":      _s(r.get("TipoDespacho")),
        "forma_pago":         _s(r.get("FormaPago")),
        "fecha_creacion":     _dt(r.get("FechaCreacion")),
        "fecha_envio":        _dt(r.get("FechaEnvio")),
        "fecha_aceptacion":   _dt(r.get("FechaAceptacion")),
        "fecha_cancelacion":  _dt(r.get("FechaCancelacion")),
        "fecha_ultima_modificacion": _dt(r.get("fechaUltimaModificacion")),
        "comprador_codigo_organismo": _s(r.get("CodigoOrganismoPublico")),
        "comprador_nombre":   _s(r.get("OrganismoPublico")),
        "comprador_rut":      _s(r.get("RutUnidadCompra")),
        "comprador_codigo_unidad": _s(r.get("CodigoUnidadCompra")),
        "comprador_unidad":   _s(r.get("UnidadCompra")),
        "comprador_actividad": _s(r.get("ActividadComprador")),
        "comprador_comuna":   _s(r.get("CiudadUnidadCompra")),
        "comprador_region":   _s(r.get("RegionUnidadCompra")),
        "comprador_pais":     _s(r.get("PaisUnidadCompra")),
        "proveedor_codigo":   _s(r.get("CodigoProveedor")),
        "proveedor_nombre":   _s(r.get("NombreProveedor")),
        "proveedor_actividad": _s(r.get("ActividadProveedor")),
        "proveedor_codigo_sucursal": _s(r.get("CodigoSucursal")),
        "proveedor_nombre_sucursal": _s(r.get("Sucursal")),
        "proveedor_rut":      _s(r.get("RutSucursal")),
        "proveedor_comuna":   _s(r.get("ComunaProveedor")),
        "proveedor_region":   _s(r.get("RegionProveedor")),
        "proveedor_pais":     _s(r.get("PaisProveedor")),
        "source":             "datos_abiertos",
        "raw_hash":           f"da:{mes}",   # evita que los backfills por API la re-pidan
        "last_sync_at":       now_iso,
    }

def parse_item(r, line_no):
    return {
        "codigo_oc":        _s(r.get("Codigo")),
        "line_no":          line_no,
        "producto_codigo":  _s(r.get("codigoProductoONU")),
        "producto_nombre":  _s(r.get("NombreroductoGenerico")),
        "codigo_categoria": _s(r.get("codigoCategoria")),
        "categoria":        _s(r.get("Categoria")),
        "especificacion_comprador": _s(r.get("EspecificacionComprador")),
        "especificacion_proveedor": _s(r.get("EspecificacionProveedor")),
        "unidad":           _s(r.get("UnidadMedida")),
        "cantidad":         _num(r.get("cantidad")),
        "precio_unitario":  _num(r.get("precioNeto")),
        "total_linea":      _num(r.get("totalLineaNeto")),
        "impuestos_linea":  _num(r.get("totalImpuestos")),
        "total_cargos":     _num(r.get("totalCargos")),
        "total_descuentos": _num(r.get("totalDescuentos")),
    }

def procesar_mes(mes: str, zip_path: str):
    now_iso = datetime.now(timezone.utc).isoformat()
    z = zipfile.ZipFile(zip_path)
    name = z.namelist()[0]
    seen = set()          # OCs cuyo header ya se envió
    line_no = {}          # codigo_oc -> correlativo de items
    buf_h, buf_i = [], []
    n_rows = n_h = n_i = 0

    with z.open(name) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter=";")
        for r in reader:
            n_rows += 1
            if LIMIT and n_rows > LIMIT: break
            cod = _s(r.get("Codigo"))
            if not cod: continue

            if cod not in seen:
                seen.add(cod)
                buf_h.append(parse_header(r, mes, now_iso))
                if len(buf_h) >= 1000:
                    _sb_upsert(T_HDR, "codigo_oc", buf_h); n_h += len(buf_h); buf_h = []

            # Items: solo RUTs objetivo o con vínculo a licitación
            if _norm_rut(r.get("RutSucursal")) in RUTS_NORM or _s(r.get("CodigoLicitacion")):
                ln = line_no.get(cod, 0) + 1
                line_no[cod] = ln
                buf_i.append(parse_item(r, ln))
                if len(buf_i) >= 1000:
                    # FK: los headers pendientes deben insertarse antes que sus items
                    if buf_h:
                        _sb_upsert(T_HDR, "codigo_oc", buf_h); n_h += len(buf_h); buf_h = []
                    _sb_upsert(T_ITEMS, "codigo_oc,line_no", buf_i); n_i += len(buf_i); buf_i = []

            if n_rows % 100000 == 0:
                log.info("  %s: %s filas | headers %s | items %s", mes, f"{n_rows:,}", f"{n_h:,}", f"{n_i:,}")

    if buf_h: _sb_upsert(T_HDR, "codigo_oc", buf_h); n_h += len(buf_h)
    if buf_i: _sb_upsert(T_ITEMS, "codigo_oc,line_no", buf_i); n_i += len(buf_i)
    log.info("✅ %s: %s filas CSV → %s headers, %s items", mes, f"{n_rows:,}", f"{n_h:,}", f"{n_i:,}")

def main():
    if LOCALZIP:
        mes = os.path.basename(LOCALZIP).replace("oc-", "").replace(".zip", "")
        log.info("=== ingesta datos abiertos (LOCAL) %s ===", LOCALZIP)
        procesar_mes(mes, LOCALZIP)
        return

    log.info("=== ingesta OC datos abiertos: %s ===", MESES)
    for mes in MESES:
        url = BLOB.format(mes=mes)
        log.info("Descargando %s ...", url)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            with requests.get(url, stream=True, timeout=1800) as resp:
                if resp.status_code == 404:
                    log.warning("⏭️  %s aún no publicado (404)", mes); os.unlink(tmp.name); continue
                resp.raise_for_status()
                for chunk in resp.iter_content(1 << 20):
                    tmp.write(chunk)
            path = tmp.name
        try:
            procesar_mes(mes, path)
        finally:
            os.unlink(path)

if __name__ == "__main__":
    main()
