"""
ingesta_lic_datos_abiertos.py
=============================
Carga masiva de LICITACIONES desde Datos Abiertos de ChileCompra — sin usar la
API transaccional y sin ticket. Es el gemelo de ingesta_oc_datos_abiertos.py.

Archivo mensual (blob Azure público, 14–26 MB zip, CSV ';' en CP1252, ~340 MB):
  https://transparenciachc.blob.core.windows.net/lic-da/{AÑO}-{MES}.zip   (mes sin cero)
  Indexado por MES DE PUBLICACIÓN. Existe desde 2023-1 hasta el mes en curso.
  Los archivos se REGENERAN (el de 2025-3 tiene fecha 16-04-2026), así que traen
  la adjudicación aunque haya ocurrido meses después. Por eso el workflow recarga
  siempre los últimos tres meses.

Qué trae que la API no da: UNA FILA POR OFERTA POR LÍNEA. O sea todas las ofertas
—perdedoras incluidas— con monto, la admisibilidad (Estado Oferta) y el número
de oferentes. Es la base del termómetro de competencia a escala de mercado.

Estrategia (misma lógica que la de OC):
  - CABECERAS: TODAS las licitaciones del mes → mp_da_licitaciones. Es el
    denominador del universo. Ojo: el archivo sólo trae licitaciones con al
    menos una oferta; las desiertas sin oferentes no aparecen.
  - OFERTAS: sólo de licitaciones core (regex + rubro) o donde oferta un RUT de
    mp_competidores → mp_da_ofertas.
  - CONSOLIDACIÓN: al cerrar cada mes se llama fn_mp_da_consolidar(mes), que
    inserta lo que falta en mp_licitaciones / comprador / items / adjudicaciones
    y rellena los montos en null. Nunca pisa lo que trajo la API.

Config por env:
  LIC_DA_MESES        "2025-1,2025-2"   (default: mes vencido + dos anteriores)
  LIC_DA_LOCALZIP     ruta a un zip ya descargado (pruebas; ignora LIC_DA_MESES)
  LIC_DA_LIMIT        tope de filas CSV a procesar (smoke test; 0 = sin tope)
  LIC_DA_DRY_RUN      "1" = parsea y cuenta, no escribe en Supabase
  LIC_DA_CORE_REGEX   sobreescribe el regex de core (ver CORE_REGEX_DEFAULT)
  LIC_DA_SIN_CONSOLIDAR "1" = no llama fn_mp_da_consolidar al final
"""

import os, re, csv, io, sys, time, json, hashlib, zipfile, tempfile, logging, requests
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("lic_da")

DRY_RUN  = os.getenv("LIC_DA_DRY_RUN", "0") == "1"
LOCALZIP = os.getenv("LIC_DA_LOCALZIP") or None
LIMIT    = int(os.getenv("LIC_DA_LIMIT", "0"))
SIN_CONSOLIDAR = os.getenv("LIC_DA_SIN_CONSOLIDAR", "0") == "1"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY       = os.environ.get("SUPABASE_SERVICE_KEY", "")
SB_REST      = f"{SUPABASE_URL}/rest/v1"
if not DRY_RUN and (not SUPABASE_URL or not SB_KEY):
    sys.exit("Faltan SUPABASE_URL / SUPABASE_SERVICE_KEY (o usa LIC_DA_DRY_RUN=1)")

T_LIC, T_OF = "mp_da_licitaciones", "mp_da_ofertas"
BLOB = "https://transparenciachc.blob.core.windows.net/lic-da/{mes}.zip"

# ── Core: qué licitaciones merecen que carguemos TODAS sus ofertas ───────────
# Misma definición que la vista v_core_bti (14-09-2026). Fabricación / carrozado.
# Aljibes y limpiafosas quedan fuera a propósito: se importan, no se fabrican.
CORE_REGEX_DEFAULT = (
    r"(ambulanc|carroza|carroceri|carro bomba|carrobomba|carro de rescate|rescate vehicular"
    r"|oficina movil|unidad movil|biblioteca movil|primer ataque|primera intervencion"
    r"|puesto de mando|movilidad reducida|vehiculo inclusiv)"
)
CORE_MOVIL_REGEX = r"(clinica|sala|box|consultorio|dental|odontolog|veterinari|mamograf)"
# Rubro ONU de la línea: sólo cuenta si el rubro2 es de vehículos motorizados (el
# rubro3 "emergencia" aparece también en iluminación, construcciones prefabricadas…)
CORE_RUBRO2      = "VEHICULOS MOTORIZADOS"
CORE_RUBRO3      = "VEHICULOS DE EMERGENCIA"
SERVICIO_REGEX   = (
    r"(servicios? de traslado|traslado de pacient|traslado en ambulanc|servicios? de ambulanc|aero ?ambulanc|operativo|movilizacion|ploteo|mantenci|mantenimiento|mttos?\b|reparacion"
    r"|arriendo|arrendamiento|leasing|seguro|poliza|combustible|neumatic|lubricant|repuesto|capacitacion)"
)
# Compras PARA una ambulancia/clínica móvil que no son el vehículo: insumos, fármacos,
# bolsos, uniformes… Se evalúa sólo sobre el NOMBRE, igual que el regex de core.
EXCLUIR_REGEX    = (
    r"(insumo|farmaco|medicamento|bolso|uniforme|vestuario|alimento|material|equipamiento medico"
    r"|mobiliario|instrumental|dispositivo|articulo|utiles|kit |motocicleta|lancha)"
)
_core_rx  = re.compile(os.getenv("LIC_DA_CORE_REGEX") or CORE_REGEX_DEFAULT)
_movil_rx = re.compile(CORE_MOVIL_REGEX)
_serv_rx  = re.compile(SERVICIO_REGEX)
_excl_rx  = re.compile(EXCLUIR_REGEX)
_TRANS = str.maketrans("áéíóúÁÉÍÓÚñÑüÜ", "aeiouAEIOUnNuU")

def _norm(s):
    return (s or "").translate(_TRANS).lower()

def es_core(nombre, descripcion, rubro2, rubro3):
    """Prefiltro de carga: decide si vale la pena guardar TODAS las ofertas de la
    licitación. Se evalúa sobre el nombre (la descripción mete demasiado ruido:
    recall medido igual, 43/49 adjudicaciones de BTI, con o sin ella). La
    definición analítica del core vive en la vista v_core_bti y se afina allá."""
    n = _norm(nombre)
    if _serv_rx.search(n) or _excl_rx.search(n):
        return False
    if _core_rx.search(n):
        return True
    if _movil_rx.search(n) and "movil" in n:
        return True
    return (_norm(rubro2).upper() == CORE_RUBRO2 and _norm(rubro3).upper() == CORE_RUBRO3)

def cargar_ofertas(nombre, descripcion, rubro2):
    """Red ancha: ¿guardamos las ofertas de esta licitación aunque no sea core estricto?
    Sí si el nombre O la descripción hablan de carrozado/ambulancia/clínica móvil, o si
    la línea es de vehículos motorizados (cualquier rubro3). Medido 14-09-2026: con el
    prefiltro estricto, 607 de 617 adjudicaciones core de 2025 quedaron sin monto porque
    la licitación no calificaba como core y sus ofertas nunca se cargaron. Cargar de más
    cuesta unas 2.000 filas/mes; cargar de menos deja el análisis sin montos."""
    txt = _norm(nombre) + " " + _norm(descripcion)
    if _core_rx.search(txt):
        return True
    if _movil_rx.search(txt) and "movil" in txt:
        return True
    return _norm(rubro2).upper() == CORE_RUBRO2

# RUTs objetivo (grupo + competidores) desde mp_competidores
RUTS_NORM = set()
if not DRY_RUN:
    from competidores import ruts_norm, norm_rut
    RUTS_NORM = ruts_norm()
else:
    def norm_rut(v): return (v or "").replace(".", "").replace("-", "").strip().upper()

# ── Parsers ──────────────────────────────────────────────────────────────────
_RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}")

def _s(v):
    s = (v or "").strip()
    return None if s in ("", "NA", "NULL", "null") else s

def _num(v):
    s = (v or "").strip()
    if not s or s == "NA": return None
    try:
        return float(s.replace(",", ".")) if ("," in s and "." not in s) else float(s)
    except ValueError:
        return None

def _int(v):
    n = _num(v)
    return int(n) if n is not None else None

def _dt(v):
    s = (v or "").strip()
    return s if _RE_FECHA.match(s) else None

_MONEDA = {"peso chileno": "CLP", "dolar": "USD", "dólar": "USD", "unidad de fomento": "CLF",
           "euro": "EUR", "moneda revisar": "UTM", "utm": "UTM"}

def _moneda(codigo, nombre):
    """Prefiere el código ISO de la columna CodigoMoneda; si no, traduce el nombre."""
    c = _s(codigo)
    if c and len(c) == 3: return c.upper()
    n = (_s(nombre) or "").lower()
    return _MONEDA.get(n, _s(nombre))

def _bool_sel(v):
    return (v or "").strip().lower() == "seleccionada"

def _hash(*parts):
    return hashlib.md5("|".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()

def parse_licitacion(r, mes, core):
    return {
        "codigo_externo":       _s(r.get("CodigoExterno")),
        "codigo_interno":       _int(r.get("Codigo")),
        "link":                 _s(r.get("Link")),
        "nombre":               _s(r.get("Nombre")),
        "descripcion":          _s(r.get("Descripcion")),
        "criterios_evaluacion": _s(r.get("CriteriosEvaluacion")),
        "tipo_adquisicion":     _s(r.get("Tipo de Adquisicion")),
        "codigo_estado":        _int(r.get("CodigoEstado")),
        "estado":               _s(r.get("Estado")),
        "codigo_organismo":     _s(r.get("CodigoOrganismo")),
        "nombre_organismo":     _s(r.get("NombreOrganismo")),
        "sector":               _s(r.get("sector")),
        "rut_unidad":           _s(r.get("RutUnidad")),
        "codigo_unidad":        _s(r.get("CodigoUnidad")),
        "nombre_unidad":        _s(r.get("NombreUnidad")),
        "direccion_unidad":     _s(r.get("DireccionUnidad")),
        "comuna_unidad":        _s(r.get("ComunaUnidad")),
        "region_unidad":        _s(r.get("RegionUnidad")),
        "codigo_tipo":          _int(r.get("CodigoTipo")),
        "tipo":                 _s(r.get("Tipo")),
        "tipo_convocatoria":    _s(r.get("TipoConvocatoria")),
        "moneda":               _moneda(r.get("CodigoMoneda"), r.get("Moneda Adquisicion")),
        "etapas":               _int(r.get("Etapas")),
        "cantidad_reclamos":    _int(r.get("CantidadReclamos")),
        "fecha_creacion":       _dt(r.get("FechaCreacion")),
        "fecha_publicacion":    _dt(r.get("FechaPublicacion")),
        "fecha_cierre":         _dt(r.get("FechaCierre")),
        "fecha_acto_apertura_economica": _dt(r.get("FechaActoAperturaEconomica")),
        "fecha_adjudicacion":   _dt(r.get("FechaAdjudicacion")),
        "fecha_estimada_adjudicacion": _dt(r.get("FechaEstimadaAdjudicacion")),
        "monto_estimado":       _num(r.get("MontoEstimado")),
        "visibilidad_monto":    _s(r.get("VisibilidadMonto")),
        "fuente_financiamiento": _s(r.get("FuenteFinanciamiento")),
        "numero_oferentes":     _int(r.get("NumeroOferentes")),
        "es_core":              core,
        "mes_archivo":          mes,
    }

def parse_oferta(r, mes):
    cod = _s(r.get("CodigoExterno"))
    row = {
        "codigo_externo":           cod,
        "correlativo":              _int(r.get("Correlativo")),
        "codigo_item":              _int(r.get("Codigoitem")),
        "codigo_producto_onu":      _int(r.get("CodigoProductoONU")),
        "rubro1":                   _s(r.get("Rubro1")),
        "rubro2":                   _s(r.get("Rubro2")),
        "rubro3":                   _s(r.get("Rubro3")),
        "nombre_producto_generico": _s(r.get("Nombre producto genrico")),
        "nombre_linea":             _s(r.get("Nombre linea Adquisicion")),
        "descripcion_linea":        _s(r.get("Descripcion linea Adquisicion")),
        "unidad_medida":            _s(r.get("UnidadMedida")),
        "cantidad":                 _num(r.get("Cantidad")),
        "codigo_proveedor":         _s(r.get("CodigoProveedor")),
        "codigo_sucursal":          _s(r.get("CodigoSucursalProveedor")),
        "rut_proveedor":            _s(r.get("RutProveedor")),
        "nombre_proveedor":         _s(r.get("NombreProveedor")),
        "razon_social_proveedor":   _s(r.get("RazonSocialProveedor")),
        "nombre_oferta":            _s(r.get("Nombre de la Oferta")),
        "estado_oferta":            _s(r.get("Estado Oferta")),
        "cantidad_ofertada":        _num(r.get("Cantidad Ofertada")),
        "moneda_oferta":            _moneda(r.get("CodigoMoneda"), r.get("Moneda de la Oferta")),
        "monto_unitario_oferta":    _num(r.get("MontoUnitarioOferta")),
        "valor_total_ofertado":     _num(r.get("Valor Total Ofertado")),
        "cantidad_adjudicada":      _num(r.get("CantidadAdjudicada")),
        "monto_linea_adjudica":     _num(r.get("MontoLineaAdjudica")),
        "fecha_envio_oferta":       _dt(r.get("FechaEnvioOferta")),
        "oferta_seleccionada":      _bool_sel(r.get("Oferta seleccionada")),
        "mes_archivo":              mes,
    }
    # Clave estable: licitación + línea + proveedor/sucursal + nombre de oferta.
    row["line_hash"] = _hash(cod, row["correlativo"], row["codigo_item"],
                             row["codigo_proveedor"], row["codigo_sucursal"], row["nombre_oferta"])
    return row

# ── Supabase ─────────────────────────────────────────────────────────────────
def _sb_headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}

def _sb_upsert(table, on_conflict, rows, retries=4):
    if not rows or DRY_RUN: return
    for i in range(retries):
        r = requests.post(f"{SB_REST}/{table}", headers=_sb_headers(),
                          params={"on_conflict": on_conflict}, json=rows, timeout=180)
        if r.ok: return
        log.warning("SB %s intento %d (%s): %s", table, i + 1, r.status_code, r.text[:300])
        time.sleep(4 * (i + 1))
    raise RuntimeError(f"upsert {table} falló tras {retries} intentos")

def _sb_rpc(fn, payload):
    if DRY_RUN: return []
    r = requests.post(f"{SB_REST}/rpc/{fn}", headers=_sb_headers() | {"Prefer": "return=representation"},
                      json=payload, timeout=600)
    if not r.ok:
        raise RuntimeError(f"rpc {fn}: {r.status_code} {r.text[:300]}")
    return r.json() if r.text else []

# ── Proceso de un mes ────────────────────────────────────────────────────────
def procesar_mes(mes: str, zip_path: str):
    t0 = time.time()
    z = zipfile.ZipFile(zip_path)
    name = z.namelist()[0]
    log.info("  %s: abriendo %s (%s MB descomprimido)", mes, name, f"{z.getinfo(name).file_size / 1e6:,.0f}")

    core_por_lic = {}            # codigo_externo -> bool  es_core estricto (flag analítico)
    cargar_por_lic = {}          # codigo_externo -> bool  red ancha: ¿guardar sus ofertas?
    # Los buffers son dicts por clave: PostgREST hace un solo INSERT ... ON CONFLICT y
    # Postgres rechaza dos filas con la misma clave en el mismo comando (error 21000).
    buf_l, buf_o = {}, {}        # codigo_externo -> fila | line_hash -> fila
    promos = {}                  # cabeceras ya enviadas que después se promovieron a core
    n_rows = n_l = n_o = n_core = n_comp = 0
    ejemplos_core = []

    def flush_l():
        nonlocal buf_l, n_l
        if buf_l:
            _sb_upsert(T_LIC, "codigo_externo", list(buf_l.values())); n_l += len(buf_l); buf_l = {}

    def flush_o():
        nonlocal buf_o, n_o
        if buf_o:
            _sb_upsert(T_OF, "line_hash", list(buf_o.values())); n_o += len(buf_o); buf_o = {}

    with z.open(name) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="cp1252", errors="replace"), delimiter=";")
        for r in reader:
            n_rows += 1
            if LIMIT and n_rows > LIMIT: break
            cod = _s(r.get("CodigoExterno"))
            if not cod: continue

            # Cabecera: una vez por licitación. El core se decide por nombre/descr/rubro
            # de la primera línea; si una línea posterior es de emergencia, se promueve.
            if cod not in core_por_lic:
                core = es_core(r.get("Nombre"), r.get("Descripcion"), r.get("Rubro2"), r.get("Rubro3"))
                core_por_lic[cod] = core
                cargar_por_lic[cod] = core or cargar_ofertas(r.get("Nombre"), r.get("Descripcion"), r.get("Rubro2"))
                buf_l[cod] = parse_licitacion(r, mes, core)
                if core:
                    n_core += 1
                    if len(ejemplos_core) < 5: ejemplos_core.append(f"{cod} · {(r.get('Nombre') or '')[:60]}")
                if len(buf_l) >= 1000: flush_l()
            elif not core_por_lic[cod] and es_core(r.get("Nombre"), None, r.get("Rubro2"), r.get("Rubro3")):
                core_por_lic[cod] = True; cargar_por_lic[cod] = True; n_core += 1
                fila = parse_licitacion(r, mes, True)
                if cod in buf_l: buf_l[cod] = fila          # aún no se envió: se reemplaza en el buffer
                else:            promos[cod] = fila         # ya se envió: se re-upserta al final
            elif not cargar_por_lic[cod] and _norm(r.get("Rubro2")).upper() == CORE_RUBRO2:
                cargar_por_lic[cod] = True                  # una línea posterior es de vehículos

            # Ofertas: licitación core / red ancha, o RUT objetivo ofertando
            es_comp = norm_rut(r.get("RutProveedor")) in RUTS_NORM
            if cargar_por_lic[cod] or es_comp:
                if es_comp and not cargar_por_lic[cod]: n_comp += 1
                of = parse_oferta(r, mes)
                buf_o[of["line_hash"]] = of
                if len(buf_o) >= 1000:
                    flush_l()      # FK: cabeceras antes que ofertas
                    flush_o()

            if n_rows % 50000 == 0:
                log.info("  %s: %s filas | lic %s | core %s | ofertas %s",
                         mes, f"{n_rows:,}", f"{len(core_por_lic):,}", n_core, f"{n_o + len(buf_o):,}")

    flush_l()
    if promos:
        _sb_upsert(T_LIC, "codigo_externo", list(promos.values()))
        log.info("  %s: %d cabeceras promovidas a core por rubro de una línea posterior", mes, len(promos))
    flush_o()

    n_cargar = sum(1 for v in cargar_por_lic.values() if v)
    log.info("✅ %s: %s filas CSV → %s licitaciones (%s core estricto, %s con ofertas cargadas, %s sólo por competidor), %s ofertas · %.0fs",
             mes, f"{n_rows:,}", f"{len(core_por_lic):,}", n_core, n_cargar, n_comp, f"{n_o:,}", time.time() - t0)
    for e in ejemplos_core: log.info("     core ej: %s", e)

    if SIN_CONSOLIDAR:
        log.info("  (consolidación omitida por LIC_DA_SIN_CONSOLIDAR)")
        return
    if DRY_RUN:
        log.info("  (dry-run: no se consolida)")
        return
    res = _sb_rpc("fn_mp_da_consolidar", {"p_mes": mes})
    for paso in res:
        log.info("  consolidar %s → %s: %s", mes, paso.get("paso"), f"{paso.get('filas', 0):,}")

def descargar(mes: str) -> str | None:
    url = BLOB.format(mes=mes)
    log.info("Descargando %s ...", url)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with requests.get(url, stream=True, timeout=1800) as resp:
            if resp.status_code == 404:
                log.warning("⏭️  %s no publicado (404)", mes); tmp.close(); os.unlink(tmp.name); return None
            resp.raise_for_status()
            for chunk in resp.iter_content(1 << 20):
                tmp.write(chunk)
        tmp.close()
        return tmp.name
    except Exception:
        tmp.close(); os.unlink(tmp.name); raise

def meses_default():
    """Mes vencido + los dos anteriores (los archivos se regeneran)."""
    hoy = datetime.now(timezone.utc)
    y, m = hoy.year, hoy.month
    out = []
    for _ in range(3):
        m -= 1
        if m == 0: m = 12; y -= 1
        out.append(f"{y}-{m}")
    return list(reversed(out))

def main():
    if LOCALZIP:
        mes = os.path.basename(LOCALZIP).replace("lic-", "").replace("lic_", "").replace(".zip", "")
        log.info("=== ingesta LIC datos abiertos (LOCAL) %s → mes %s%s ===", LOCALZIP, mes, " [DRY-RUN]" if DRY_RUN else "")
        procesar_mes(mes, LOCALZIP)
        return

    meses = [m.strip() for m in (os.getenv("LIC_DA_MESES") or "").split(",") if m.strip()] or meses_default()
    log.info("=== ingesta LIC datos abiertos: %s%s ===", meses, " [DRY-RUN]" if DRY_RUN else "")
    fallos = []
    for mes in meses:
        try:
            path = descargar(mes)
            if not path: continue
            try:
                procesar_mes(mes, path)
            finally:
                os.unlink(path)
        except Exception as e:
            log.error("❌ %s: %r", mes, e)
            fallos.append(mes)
    if fallos:
        sys.exit(f"Meses con error: {fallos}")

if __name__ == "__main__":
    main()
