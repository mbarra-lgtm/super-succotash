"""
ingesta_pac.py
==============
Cuarta vía del radar: el Plan Anual de Compras (PAC) de Mercado Público.

Cada organismo publica en enero lo que piensa comprar en el año, línea por
línea, con monto y mes estimado. Es la señal más temprana que existe dentro
de Mercado Público: la línea aparece meses antes que la licitación. Pero el
PAC es indicativo (no obliga a comprar, y el organismo lo modifica cuando
quiere), así que se trata como intención, no como compromiso:
etapa 'idea' y confianza 0,6.

Medido el 23-11-2026 con "ambulancias" en el buscador público: de las líneas
de 2026 cuyo mes ya pasó, cerca de un tercio terminó en una licitación del
mismo organismo. Por eso el valor está en dos lugares:
  1. Líneas *planificadas* (mes futuro): aviso temprano para preparar oferta.
  2. Líneas *vencidas sin licitar*: el organismo tenía el dinero en el plan y
     no salió a comprar. Es un llamado comercial, no una alerta.

Flujo
-----
  buscador PAC ──> core_pac_lineas ──fn_core_pac_procesar()──> core_pac_lineas
  (rubros +        (foto del día:     organismo, región,          (estado, codigo_mp)
   palabras)        upsert por clave) retiradas, reprogramadas,          │
                                      licitación en mp_licitaciones      ▼
                                                                   core_senales
                                                                   (fuente_tipo='plan_compras')

Endpoint (público, sin ticket):
  GET /Home/PlanDeCompra/VerExcelOportunidades?PalabrasClave=..&RubrosSeleccion=..
  Devuelve una tabla HTML con extensión .xls. pagina=0 trae todas las filas.
  Rubros: 173 = [29-03] Adquisición de Vehículos, 191 = [31-02-007] Vehículos
  de Proyectos. La búsqueda por palabra es literal por palabra completa:
  "ambulancia" trae 177 filas y "ambulancias" 205, por eso van ambas formas.

Las líneas del PAC no tienen ID. La clave es md5(organismo|nombre|total|mes).
Si el organismo cambia el mes o el monto, la línea vieja desaparece y aparece
una nueva: fn_core_pac_procesar() las une (estado 'reprogramada') y la señal
se mueve a la línea nueva en vez de duplicarse.

La clasificación (¿es del rubro Bertonati? ¿qué categoría?) se hace aquí con
regex, sin modelo: los nombres de línea son cortos y el patrón es estable.
El cruce con licitaciones y organismos se hace en SQL, porque mp_licitaciones
tiene 350 mil filas y no tiene sentido traerlas por REST.

Uso:
    python ingesta_pac.py                 # corre si a la fuente le toca (frecuencia_horas)
    python ingesta_pac.py --forzar        # corre aunque no le toque
    python ingesta_pac.py --dry-run       # baja y clasifica, no escribe nada
    python ingesta_pac.py --anio 2027     # el PAC 2027 se publica en enero
"""

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("pac")

# ── Config ──────────────────────────────────────────────────────────────────
SLUG           = "pac-mercadopublico"
URL_EXPORT     = "https://www.mercadopublico.cl/Home/PlanDeCompra/VerExcelOportunidades"
URL_BUSCADOR   = "https://www.mercadopublico.cl/Home/PlanDeCompra/ResultadoBusquedaOportunidades"
PROMPT_VERSION = "pac-v1"          # versión de las reglas de clasificación

UA      = "BertonatiRadar/1.0 (+https://bertonati.cl; monitoreo de compras publicas)"
HEADERS = {"User-Agent": UA}

PAUSA_S          = float(os.getenv("PAC_PAUSA_S", "2"))
MONTO_MIN        = int(os.getenv("PAC_MONTO_MIN", "20000000"))     # bajo esto no es un vehículo
DIAS_VENCIDA_MAX = int(os.getenv("PAC_DIAS_VENCIDA_MAX", "180"))   # vencidas más viejas: sólo tabla
TIMEOUT          = 120

T_LIN = "core_pac_lineas"
T_SEN = "core_senales"

# Rubros (RubrosSeleccion del buscador) y palabras clave. Una línea que
# aparece en varias consultas se guarda una vez, con todas en `consultas`.
RUBROS = {
    "173": "[29-03] Adquisición de Vehículos",
    "191": "[31-02-007] Vehículos de Proyectos",
}
#
# El buscador distingue tildes: "retén móvil" trae filas y "reten movil" no;
# "clinica" trae 198 y "clínica" 108 distintas (25-11-2026). Por eso cada
# término con tilde va en las dos formas.
PALABRAS = [
    "ambulancia", "ambulancias", "samu",
    "clinica", "clínica", "clinicas moviles", "clínicas móviles",
    "veterinaria movil", "veterinaria móvil", "dental movil", "dental móvil",
    "unidad movil", "unidad móvil", "unidades moviles", "unidades móviles",
    "mamografo", "mamógrafo",
    "carros bomba", "bomberos", "rescate", "carro de rescate",
    "vehiculo de emergencia", "vehículo de emergencia", "vehículos de emergencia",
    "oficina movil", "oficina móvil", "oficinas moviles", "oficinas móviles",
    "centro de mando", "puesto de mando", "retén móvil",
    "blindado", "blindados", "carroceria", "carrocería", "carrozado",
    "minibus", "minibús", "minibuses", "furgon", "furgón", "furgones",
]

# ── Clasificación ───────────────────────────────────────────────────────────
# Todo se evalúa sobre el nombre sin tildes y en minúsculas (_norm).

# Partes, equipos y servicios que comparten palabra con un vehículo.
EXCLUIR = re.compile(
    r"mantenc|mantenim|reparaci|arriendo|arrendamiento|servicio|seguro|combustible|"
    r"repuesto|neumatic|lavado|insumo|uniforme|vestuario|capacitaci|curso|"
    r"camilla|monitor|pastilla|radiador|rotula|amortiguador|bateria|terminal|"
    r"notebook|impresora|computador|tubo|mesa |articulos|memoria|patrimonio|"
    r"atencion|otoscopio|inmoviliz|accesorio|\bgps\b|baliza|sirena|ploteo|rotulaci|"
    r"desfibrilador|oxigeno|equipamiento|\\bequipos?\\b|traslado de|pasajes|"
    r"conversion a gas|revision tecnica|permiso de circulacion|"
    # Obras y proyectos que nombran una base SAMU o una central de ambulancias.
    r"construcci|\bbases? samu|grupos? electrogeno|diseno|estudio|ampliaci|normalizaci|"
    r"buses? electric")

# Palabra de vehículo: exigida cuando la línea no es de un rubro de vehículos.
VEHICULO = re.compile(
    r"vehicul|vehuculo|ambulanc|carro|camion|furgon|minibus|\bbus(es)?\b|trailer|"
    r"remolque|semirremolque|\bvan\b|clinica|unidad(es)? movil|oficina(s)? movil|"
    r"\bsamu\b|\bae[ab]\b")

# (categoría core_senales, encaje, patrón del nombre de la línea, patrón para
#  reconocer la licitación correspondiente en mp_licitaciones). El orden
# importa: gana la primera que calza.
CATEGORIAS = [
    ("ambulancia", "alto",
     r"ambulanc|\bae[ab]\b|\bsamu\b|"
     r"emergencia.*(hospital|salud|cesfam|\bsar\b|posta|sapu)|"
     r"(hospital|salud|cesfam|\bsar\b|posta|sapu).*emergencia",
     r"ambulanc|\mAE[AB]\M|\mSAMU\M|emergencia"),
    ("clinica_movil", "alto",
     r"clinica.{0,40}movil|movil.{0,20}clinica|veterinari[ao] movil|dental movil|"
     r"oftalmologica movil|unidad(es)? medica(s)? movil|mamografo movil|box movil|"
     r"laboratorio movil|operativo movil",
     r"cl[ií]nica|veterinari|dental|oftalmol|unidad m[eé]dica|mam[oó]grafo|box m[oó]vil|laboratorio m[oó]vil"),
    ("blindado", "alto",
     r"blindad|lanza ?aguas|carro (celular|de traslado de (internos|detenidos))",
     r"blindad|lanza ?aguas|celular|internos|detenidos"),
    ("carro_bomba", "alto",
     r"carro bomba|carros bomba|combate de incendio|material mayor|forestal|"
     r"carro de (agua|ataque)|cuerpo de bomberos|unidad de bomberos",
     r"carro|bomba|incendio|bomberos|forestal|material mayor"),
    ("rescate", "alto",
     r"rescate|emergencia",
     r"rescate|emergencia"),
    ("carroceria", "alto",
     r"oficina(s)? movil|centro de mando|puesto de mando|unidad(es)? movil|"
     r"trailer|carrocer|carrozad|biblioteca movil|cuartel movil|habilitad",
     r"oficina|mando|m[oó]vil|trailer|carrocer|carrozad|habilitad"),
    ("movil_policial", "alto",
     r"reten movil|seguridad (ciudadana|publica|comunitaria)|patrull|carabineros|"
     r"\bpdi\b|policia",
     r"ret[eé]n|seguridad|patrull|carabineros|\mPDI\M|polic"),
    # Transporte de personas: Bertonati no fabrica buses, pero sí minibuses y
    # furgones adaptados. Encaje medio: van al panel, casi nunca al correo.
    ("otro", "medio",
     r"minibus|furgon|\bvan\b|bus (inclusivo|adaptado)|rampa|silla de ruedas|discapacidad",
     r"minib[uú]s|furg[oó]n|\mvan\M|inclusiv|adaptad|rampa|discapacidad"),
]
_CATS = [(c, e, re.compile(p), pm) for c, e, p, pm in CATEGORIAS]
PRIORIDAD_POR_ENCAJE = {"alto": 2, "medio": 4}

CONFIANZA = {"planificada": 0.6, "vencida_sin_licitar": 0.35, "licitada": 0.9}

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _hoy_cl() -> date:
    return (datetime.now(timezone.utc) - timedelta(hours=3)).date()


def _norm(t: str) -> str:
    return (t or "").lower().translate(str.maketrans("áéíóúüñ", "aeiouun"))


def _espacios(t: str) -> str:
    return " ".join((t or "").split())


def clasificar(nombre: str, linea: str, total: float) -> tuple[str | None, str | None, str | None]:
    """→ (categoria, encaje, patron_licitacion) o (None, None, None) si no es core."""
    n = _norm(nombre)
    if not total or total < MONTO_MIN or EXCLUIR.search(n):
        return None, None, None
    l = _norm(linea)
    es_rubro_vehiculo = l.startswith("[29-03]") or l.startswith("[31-02-007]")
    if not es_rubro_vehiculo:
        # Fuera de rubro sólo si nombra un vehículo y la cuenta es de inversión
        # o "Otros" (hay ambulancias imputadas a obras civiles o a equipos).
        if not VEHICULO.search(n) or not (l.startswith("[29") or l.startswith("[31") or l == "otros"):
            return None, None, None
    for cat, encaje, patron, patron_mp in _CATS:
        if patron.search(n):
            # "rescate"/"emergencia" sueltos fuera de rubro son ruido ("rescate del patrimonio").
            if cat in ("rescate", "carroceria", "movil_policial") and not VEHICULO.search(n) \
                    and not es_rubro_vehiculo:
                continue
            return cat, encaje, patron_mp
    return None, None, None


# ── Descarga ────────────────────────────────────────────────────────────────

def _num(txt: str, miles: bool) -> float | None:
    t = (txt or "").strip()
    if not t:
        return None
    if miles:
        t = t.replace(".", "")
    t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _fecha(txt: str) -> date | None:
    m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})", (txt or "").strip())
    return date(int(m[3]), int(m[2]), int(m[1])) if m else None


def consultar(session: requests.Session, anio: int, palabra: str = " ", rubro: str = "0") -> list[dict]:
    params = {
        "PalabrasClave": palabra, "RubrosSeleccion": rubro, "TipoCompraSeleccion": "0",
        "MontoDesde": "0", "MontoHasta": "999999999999",
        "FechaDesde": f"01-01-{anio}", "FechaHasta": f"31-12-{anio}", "pagina": "0",
    }
    r = session.get(URL_EXPORT, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    if "<td" not in r.text:
        # Sin resultados el export devuelve "<div>\r\n\r\n</div>" (15 bytes).
        # Cualquier otra cosa sin filas es una página de error o un cambio.
        if r.text.strip().startswith("<div") and len(r.text) < 2000:
            return []
        raise RuntimeError(f"respuesta inesperada ({len(r.text)} bytes): {r.text[:120]!r}")
    sopa = BeautifulSoup(r.text, "html.parser")
    tabla = sopa.find("table")
    cab = [th.get_text(strip=True) for th in tabla.find_all("th")]
    esperadas = {"Nombre", "Cantidad", "Monto", "FechaInicioCompra", "Total", "Organizacion",
                 "DescLineaProductoServicio", "DescTipoCompra"}
    if not esperadas <= set(cab):
        raise RuntimeError(f"cambió el formato del export: {cab}")
    filas = []
    for tr in tabla.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != len(cab):
            continue
        f = dict(zip(cab, (_espacios(td.get_text()) for td in tds)))
        filas.append({
            "nombre": f["Nombre"],
            "cantidad": _num(f["Cantidad"], miles=True),
            "monto_unitario": _num(f["Monto"], miles=True),
            "cantidad_oc": int(_num(f.get("CantidadOC"), miles=True) or 0) or None,
            "fecha_inicio": _fecha(f["FechaInicioCompra"]),
            # Total viene sin separador de miles ("155000000"); a veces con coma decimal.
            "total": _num(f["Total"], miles=False),
            "organizacion": f["Organizacion"],
            "linea_producto": f["DescLineaProductoServicio"],
            "tipo_compra": f["DescTipoCompra"],
        })
    return filas


def clave_linea(f: dict) -> str:
    base = "|".join([
        _espacios(f["organizacion"]).upper(), _espacios(f["nombre"]).upper(),
        f"{round(f['total'] or 0)}", f["fecha_inicio"].isoformat() if f["fecha_inicio"] else "",
    ])
    return hashlib.md5(base.encode()).hexdigest()


def cosechar(session: requests.Session, anio: int) -> tuple[dict, list]:
    """Corre todas las consultas. → ({clave: línea}, [consultas fallidas])."""
    consultas = [(f"rubro:{c}", " ", c) for c in RUBROS] + [(p, p, "0") for p in PALABRAS]
    lineas, fallidas = {}, []
    for etiqueta, palabra, rubro in consultas:
        try:
            filas = consultar(session, anio, palabra, rubro)
        except Exception as e:
            log.warning("   consulta '%s' falló: %s", etiqueta, e)
            fallidas.append(etiqueta)
            time.sleep(PAUSA_S)
            continue
        nuevas = 0
        for f in filas:
            if not f["fecha_inicio"] or f["fecha_inicio"].year != anio:
                continue
            k = clave_linea(f)
            if k not in lineas:
                f["linea_key"] = k
                f["consultas"] = []
                lineas[k] = f
                nuevas += 1
            lineas[k]["consultas"].append(etiqueta)
        log.info("   %-24s %4d filas, %4d nuevas", etiqueta, len(filas), nuevas)
        time.sleep(PAUSA_S)
    return lineas, fallidas


# ── Escritura ───────────────────────────────────────────────────────────────

def fila_tabla(f: dict, anio: int, ahora: str) -> dict:
    cat, encaje, patron_mp = clasificar(f["nombre"], f["linea_producto"], f["total"])
    return {
        "linea_key": f["linea_key"], "anio": anio,
        "organizacion": f["organizacion"][:300], "nombre": f["nombre"][:1000],
        "cantidad": f["cantidad"], "monto_unitario": f["monto_unitario"],
        "cantidad_oc": f["cantidad_oc"], "total": f["total"],
        "fecha_inicio": f["fecha_inicio"].isoformat(),
        "linea_producto": f["linea_producto"][:300], "tipo_compra": f["tipo_compra"][:200],
        "consultas": f["consultas"],
        "es_core": cat is not None, "categoria": cat, "encaje": encaje, "patron_mp": patron_mp,
        "reglas_version": PROMPT_VERSION,
        "last_seen_at": ahora, "removed_at": None,
    }


def _mes(d: date) -> str:
    return f"{MESES[d.month - 1]} {d.year}"


def _clp(v) -> str:
    return f"${float(v):,.0f}".replace(",", ".") if v else "monto no informado"


def textos_senal(l: dict) -> tuple[str, str, str]:
    """→ (titulo, resumen, por_que_importa) según el estado de la línea."""
    org = l.get("organismo") or l["organizacion"]
    nombre = _espacios(l["nombre"])
    titulo = f"PAC {l['anio']} · {org} — {nombre}"
    fecha = date.fromisoformat(l["fecha_inicio"])
    resumen = (f"{nombre} · {_clp(l['total'])}"
               + (f" · {int(l['cantidad'])} un." if l.get("cantidad") and l["cantidad"] > 1 else "")
               + f" · compra planificada para {_mes(fecha)} · {l['linea_producto']}"
               + (f" · {l['tipo_compra']}" if l.get("tipo_compra") else ""))
    estado = l["estado"]
    if estado == "licitada":
        pq = (f"Ya salió a licitación: {l['codigo_mp']}"
              + (f" ({l['licitacion_nombre']})" if l.get("licitacion_nombre") else "")
              + (f", publicada el {date.fromisoformat(l['licitacion_fecha'][:10]).strftime('%d-%m-%Y')}"
                 if l.get("licitacion_fecha") else "") + ".")
    elif estado == "vencida_sin_licitar":
        pq = (f"Estaba en el Plan Anual de Compras para {_mes(fecha)} y no aparece una licitación "
              "del organismo para esto. Puede haberse postergado, comprado por convenio marco o "
              "trato directo, o descartado: vale una llamada para saber si el dinero sigue ahí.")
    elif estado == "retirada":
        pq = "El organismo sacó esta línea de su Plan Anual de Compras."
    else:
        pq = (f"El organismo la declaró en su Plan Anual de Compras para {_mes(fecha)}. "
              "El PAC es indicativo: cerca de un tercio de estas líneas termina en licitación, "
              "pero avisa con meses de anticipación.")
    if l.get("reprograma_de_fecha"):
        pq = f"Reprogramada desde {_mes(date.fromisoformat(l['reprograma_de_fecha']))}. " + pq
    return titulo[:500], resumen[:2000], pq[:2000]


def _score(l: dict, etapa: str, confianza: float) -> int:
    return sb.rpc("fn_core_score", {
        "p_etapa": etapa,
        "p_unidades": int(l["cantidad"]) if l.get("cantidad") and l["categoria"] != "otro" else None,
        "p_monto_clp": l["total"], "p_prioridad": PRIORIDAD_POR_ENCAJE.get(l["encaje"], 3),
        "p_confianza": confianza, "p_fecha_evento": l["fecha_inicio"]})


def sincronizar_senales(fuente: dict, anio: int) -> dict:
    """Crea señales para líneas core nuevas y actualiza las existentes cuando
    la línea cambió de estado o de mes."""
    hoy = _hoy_cl()
    lineas = sb.select("v_core_pac_lineas", {
        "select": "*", "anio": f"eq.{anio}", "es_core": "is.true",
        "senal_sincronizada": "is.false", "order": "fecha_inicio.asc"})
    cuenta = {"nuevas": 0, "actualizadas": 0, "sin_senal": 0}
    ahora = datetime.now(timezone.utc).isoformat()

    for l in lineas:
        estado = l["estado"]
        etapa = "licitacion_publicada" if estado == "licitada" else "idea"
        confianza = CONFIANZA.get(estado, 0.3)
        titulo, resumen, pq = textos_senal(l)

        if not l.get("senal_id"):
            vieja = date.fromisoformat(l["fecha_inicio"]) < hoy - timedelta(days=DIAS_VENCIDA_MAX)
            if estado not in ("planificada", "vencida_sin_licitar") or \
                    (estado == "vencida_sin_licitar" and vieja):
                # Ya licitada al detectarla, o vencida hace mucho: queda en la
                # tabla para estadística, no merece señal.
                sb.update(T_LIN, {"id": f"eq.{l['id']}"}, {"senal_estado": estado,
                                                           "senal_fecha": l["fecha_inicio"]})
                cuenta["sin_senal"] += 1
                continue
            fila = {
                "source_id": fuente["id"], "organismo_id": l.get("organismo_id"),
                "fuente_tipo": "plan_compras", "fuente_url": URL_BUSCADOR,
                "region": l.get("region"), "comuna": l.get("comuna"),
                "categoria": l["categoria"], "etapa": etapa,
                "titulo": titulo, "resumen": resumen, "por_que_importa": pq,
                "unidades": int(l["cantidad"]) if l.get("cantidad") and l["categoria"] != "otro" else None,
                "monto_clp": l["total"], "monto_raw": _clp(l["total"]),
                "fecha_evento": l["fecha_inicio"], "confianza": confianza,
                "score": _score(l, etapa, confianza),
                "dedupe_key": sb.rpc("fn_core_dedupe_key", {
                    "p_organismo_id": l.get("organismo_id"), "p_categoria": "plan_compras",
                    "p_etapa": "idea", "p_codigo": f"pac:{l['linea_key']}",
                    "p_fecha": l["fecha_inicio"], "p_monto": l["total"]}),
                "clasificador_modelo": "reglas", "prompt_version": PROMPT_VERSION,
                "clasificado_at": ahora,
            }
            creada = sb.insert(T_SEN, [fila], on_conflict="dedupe_key", ignorar_dup=True)
            senal_id = creada[0]["id"] if creada else (sb.select(T_SEN, {
                "select": "id", "dedupe_key": f"eq.{fila['dedupe_key']}"}) or [{}])[0].get("id")
            if creada:
                cuenta["nuevas"] += 1
            sb.update(T_LIN, {"id": f"eq.{l['id']}"}, {
                "senal_id": senal_id, "senal_estado": estado, "senal_fecha": l["fecha_inicio"]})
            continue

        # Señal existente: reflejar el estado nuevo de la línea.
        cambios = {"titulo": titulo, "resumen": resumen, "por_que_importa": pq,
                   "etapa": etapa, "confianza": confianza, "score": _score(l, etapa, confianza),
                   "fecha_evento": l["fecha_inicio"], "monto_clp": l["total"],
                   "updated_at": ahora}
        if estado == "licitada":
            cambios["codigo_mp"] = l["codigo_mp"]
        if estado == "retirada" and l.get("senal_estado_gestion") == "nueva":
            cambios.update({"estado_gestion": "descartada", "gestionada_at": ahora,
                            "motivo_descarte": "Línea retirada del Plan Anual de Compras"})
        sb.update(T_SEN, {"id": f"eq.{l['senal_id']}"}, cambios)
        sb.update(T_LIN, {"id": f"eq.{l['id']}"}, {"senal_estado": estado,
                                                   "senal_fecha": l["fecha_inicio"]})
        cuenta["actualizadas"] += 1
    return cuenta


# ── Orquestación ────────────────────────────────────────────────────────────

def _le_toca(fuente: dict) -> bool:
    visto = fuente.get("last_checked_at")
    if not visto:
        return True
    t = datetime.fromisoformat(visto.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - t).total_seconds() >= (fuente.get("frecuencia_horas") or 24) * 3600


def main() -> int:
    global sb
    ap = argparse.ArgumentParser(description="Plan Anual de Compras (PAC) → radar")
    ap.add_argument("--anio", type=int, default=None,
                    help="año del plan (por defecto el actual, y hasta marzo también el anterior)")
    ap.add_argument("--slug", default=SLUG, help="fuente en core_sources")
    ap.add_argument("--forzar", action="store_true", help="correr aunque no toque por frecuencia")
    ap.add_argument("--dry-run", action="store_true", help="bajar y clasificar sin escribir")
    args = ap.parse_args()

    hoy = _hoy_cl()
    # Hasta marzo se sigue mirando el plan del año anterior: las líneas de
    # noviembre y diciembre suelen licitarse en enero o febrero.
    anios = [args.anio] if args.anio else ([hoy.year - 1] if hoy.month <= 3 else []) + [hoy.year]

    if args.dry_run and not os.getenv("SUPABASE_URL"):
        sb = None
        fuente = {"id": None, "slug": args.slug}
    else:
        import sb as _sb
        sb = _sb
        filas = sb.select("core_sources", {
            "select": "id,slug,frecuencia_horas,last_checked_at,consecutive_errors,enabled",
            "slug": f"eq.{args.slug}"})
        if not filas or not filas[0]["enabled"]:
            log.error("Fuente %s no existe o está deshabilitada (¿se aplicó la migración "
                      "2026-11-25_radar_pac.sql?).", args.slug)
            if not args.dry_run:
                return 0
            filas = [{"id": None, "slug": args.slug}]
        fuente = filas[0]
        if not args.forzar and not args.dry_run and not _le_toca(fuente):
            log.info("PAC: no le toca todavía (frecuencia %sh).", fuente.get("frecuencia_horas"))
            return 0

    t0 = time.time()
    with requests.Session() as session:
        for anio in anios:
            log.info("PAC %d: %d consultas", anio, len(RUBROS) + len(PALABRAS))
            try:
                lineas, fallidas = cosechar(session, anio)
            except Exception as e:
                log.error("PAC %d: ERROR %s", anio, e)
                if sb and not args.dry_run:
                    sb.marcar_fuente_error(fuente["id"], fuente.get("consecutive_errors"), str(e))
                return 1

            ahora = datetime.now(timezone.utc).isoformat()
            filas = [fila_tabla(f, anio, ahora) for f in lineas.values()]
            core = [f for f in filas if f["es_core"]]
            log.info("PAC %d: %d líneas únicas, %d del rubro (%s), consultas fallidas: %d",
                     anio, len(filas), len(core),
                     ", ".join(f"{c}={sum(1 for f in core if f['categoria'] == c)}"
                               for c, *_ in CATEGORIAS), len(fallidas))

            if args.dry_run:
                for f in sorted(core, key=lambda x: (x["fecha_inicio"], -(x["total"] or 0))):
                    log.info("   %s %-14s %15s  %-40.40s %s", f["fecha_inicio"][:7], f["categoria"],
                             _clp(f["total"]), f["organizacion"], f["nombre"][:70])
                continue

            if not filas:
                # Ninguna consulta trajo nada: lo más probable es que el endpoint
                # cambió. No se marcan retiradas (borraría todo el plan).
                sb.marcar_fuente_error(fuente["id"], fuente.get("consecutive_errors"),
                                       "PAC sin filas en ninguna consulta")
                return 1

            for i in range(0, len(filas), 200):
                sb.insert(T_LIN, filas[i:i + 200], on_conflict="linea_key")

            # Sólo con la cosecha completa se puede afirmar que una línea que
            # no apareció fue retirada del plan.
            res = sb.rpc("fn_core_pac_procesar", {
                "p_anio": anio, "p_corrida": ahora, "p_cosecha_completa": not fallidas})
            log.info("PAC %d: %s", anio, res)

            cuenta = sincronizar_senales(fuente, anio)
            log.info("PAC %d: señales nuevas %d, actualizadas %d, líneas sin señal %d",
                     anio, cuenta["nuevas"], cuenta["actualizadas"], cuenta["sin_senal"])

        if sb and not args.dry_run:
            sb.marcar_fuente_ok(fuente["id"], 1)

    log.info("PAC listo en %.1fs", time.time() - t0)
    return 0


sb = None

if __name__ == "__main__":
    sys.exit(main())
