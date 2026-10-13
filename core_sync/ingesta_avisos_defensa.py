"""
ingesta_avisos_defensa.py
=========================
Capa de Defensa del radar: llamados a propuesta pública del Ejército, la
Armada y la FACh que NO pasan por Mercado Público (compras de la Ley 21.174 y
exclusiones de la Ley 19.886). Se publican como avisos legales en
diarioavisoslegales.cl y El Mercurio, nunca en el portal, así que el espejo MP
no los ve.

    python ingesta_avisos_defensa.py                       # todas las fuentes aviso_legal
    python ingesta_avisos_defensa.py --slug avisos-defensa-diarioavisoslegales
    python ingesta_avisos_defensa.py --url https://diarioavisoslegales.cl/avisos/...   # un aviso a mano
    python ingesta_avisos_defensa.py --dry-run             # no escribe en Supabase

Flujo:

    Google News RSS ──> URL real del aviso ──> leer aviso + PDF ──> Claude ──> core_senales
     (descubre)         (batchexecute)          (directo, o         (tabla de     (una señal por
                                                 web_fetch)          licitaciones)  licitación)

Por qué es distinto de ingesta_prensa.py:

* **El título no dice nada.** Todos los avisos se llaman "LLAMADO A PROPUESTA
  PÚBLICA – EJÉRCITO DE CHILE División de Adquisiciones del Ejército". El
  rubro (botas, repuestos, vehículos, equipos médicos) vive en un PDF con una
  tabla. Filtrar por título como en prensa botaría todo; acá se lee el PDF de
  cada aviso, que son pocos (2 a 4 al mes).
* **diarioavisoslegales.cl está detrás de Cloudflare.** Un GET desde un runner
  de GitHub devuelve 403 "Just a moment...". Se intenta directo igual (si un
  día pasa, mejor), y si no, la lectura la hace la herramienta web_fetch de la
  API de Anthropic. No se usa ningún truco para saltarse el desafío.
* **Un aviso trae varias licitaciones.** La tabla del PDF tiene 2 a 8 filas,
  cada una con su número, rubro, correo y cierre. Cada fila es una señal.
* **El dato que manda es el cierre de solicitud de bases**, no el monto. Va en
  core_senales.fecha_limite y el correo al que se piden, en contacto.
* **Sin REPE no hay bases.** Solo empresas inscritas, habilitadas y vigentes
  en el Registro Especial de Proveedores del Ejército pueden pedirlas.

Score: misma fn_core_score del radar. El "encaje" con Bertonati entra como
prioridad (alto=1, medio=3, bajo=5): con etapa licitación (34 pts) eso deja
los de encaje alto en ~57 (llegan a todos), los medios en ~46 (comercial) y
los bajos en ~33 (solo panel, no correo). Un aviso que no se pudo leer queda
en ~37: le llega solo a quien tiene umbral 35, para que alguien lo abra.
"""

import os, re, sys, json, time, hashlib, logging, argparse
import xml.etree.ElementTree as ET
import urllib.parse as up
from datetime import date, datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

import sb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("avisos_defensa")

# ── Config ──────────────────────────────────────────────────────────────────
API            = "https://api.anthropic.com/v1/messages"
MODELO         = os.getenv("RADAR_MODELO", "claude-sonnet-4-5")
BETA_WEB_FETCH = "web-fetch-2025-09-10"
PROMPT_VERSION = "defensa-v1"

UA      = "BertonatiRadar/1.0 (+https://bertonati.cl; monitoreo de compras publicas)"
HEADERS = {"User-Agent": UA}
# Google News sirve la página de redirección sólo a navegadores.
HEADERS_GNEWS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"}

DIAS_ATRAS      = int(os.getenv("AVISOS_DIAS_ATRAS", "45"))
MAX_POR_CORRIDA = int(os.getenv("AVISOS_MAX_POR_CORRIDA", "12"))   # avisos a leer por corrida
DIAS_REINTENTO  = int(os.getenv("AVISOS_DIAS_REINTENTO", "21"))    # reintentar lecturas fallidas
MIN_CHARS_TEXTO = 200

T_DOC = "core_documents"
T_SEN = "core_senales"

# El título tiene que nombrar a una rama y no ser un remate ni otra cosa.
PATRON_DEFENSA = re.compile(
    r"ej[eé]rcito|armada|fuerza a[eé]rea|\bfach\b|divae|gendarmer[ií]a|"
    r"divisi[oó]n de (adquisiciones|ingenieros|mantenimiento)", re.I)
PATRON_RUIDO = re.compile(
    r"remat|subasta|dicrep|cr[eé]dito prendario|concurso p[uú]blico para proveer|"
    r"cargo de empleado|ej[eé]rcito evang[eé]lico|ej[eé]rcito de salvaci[oó]n|"
    r"ej[eé]rcito mexicano|wikipedia", re.I)

ORGANISMOS = {   # clave del modelo → nombre en core_organismos (Gendarmería queda sin organismo)
    "ejercito": "Ejército de Chile",
    "armada":   "Armada de Chile",
    "fach":     "Fuerza Aérea de Chile",
}
CORTO = {"ejercito": "Ejército", "armada": "Armada", "fach": "FACh", "gendarmeria": "Gendarmería"}
PRIORIDAD_POR_ENCAJE = {"alto": 1, "medio": 3, "bajo": 5}

_ORG_CACHE: dict | None = None


def _exigir_api_key() -> str:
    clave = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not clave:
        log.error("Falta ANTHROPIC_API_KEY (mismo secret que usa el resto del radar).")
        sys.exit(2)
    return clave


def _hoy_cl() -> date:
    # Chile continental: UTC-3 en verano, UTC-4 en invierno. Para comparar
    # cierres por día basta con -3; una hora de diferencia no cambia el día
    # salvo entre 00:00 y 01:00.
    return (datetime.now(timezone.utc) - timedelta(hours=3)).date()


def _norm(t: str) -> str:
    return (t or "").lower().translate(str.maketrans("áéíóúüñ", "aeiouun"))


# ── Descubrimiento (Google News) ───────────────────────────────────────────

def decodificar_gnews(url: str, session: requests.Session) -> str | None:
    """news.google.com/rss/articles/CBMi... → URL real del aviso.

    Desde 2024 el link de Google News ya no redirige con un 302: la página
    trae una firma (data-n-a-sg) y una marca de tiempo (data-n-a-ts) que hay
    que mandar al endpoint batchexecute para que devuelva la URL. Es el mismo
    llamado que hace el navegador al abrir el link.

    ingesta_prensa.py se queda con el link de Google cuando no resuelve; acá
    no se puede: sin la URL real no hay PDF que leer ni forma de deduplicar
    entre las dos fuentes.
    """
    if "news.google.com" not in url:
        return url
    try:
        aid = up.urlsplit(url).path.rsplit("/", 1)[-1]
        r = session.get(f"https://news.google.com/rss/articles/{aid}",
                        headers=HEADERS_GNEWS, timeout=20)
        r.raise_for_status()
        div = BeautifulSoup(r.text, "html.parser").select_one("c-wiz > div[jscontroller]")
        if div is None or not div.get("data-n-a-sg"):
            return None
        firma, ts = div["data-n-a-sg"], int(div["data-n-a-ts"])
        interno = ["garturlreq",
                   [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None,
                     None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                   aid, ts, firma]
        freq = [[["Fbv4je", json.dumps(interno), None, "generic"]]]
        r = session.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                         headers={**HEADERS_GNEWS,
                                  "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                         data="f.req=" + up.quote(json.dumps(freq)), timeout=20)
        r.raise_for_status()
        cuerpo = r.text.split("\n\n", 1)[1]
        return json.loads(json.loads(cuerpo)[0][2])[1]
    except Exception as e:
        log.debug("no se pudo decodificar %s: %s", url[:70], e)
        return None


def normalizar_url(url: str) -> str:
    """Quita ?print=pdf / ?print=print y utm_*: es el mismo aviso."""
    p = up.urlsplit(url.strip())
    q = [(k, v) for k, v in up.parse_qsl(p.query)
         if k != "print" and not k.startswith("utm_")]
    return up.urlunsplit((p.scheme or "https", p.netloc.lower(), p.path, up.urlencode(q), ""))


def _items(url: str, session: requests.Session) -> list:
    r = session.get(url, headers=HEADERS, timeout=45)
    r.raise_for_status()
    try:
        return list(ET.fromstring(r.content).iter("item"))
    except ET.ParseError as e:
        raise RuntimeError(f"RSS ilegible: {e}")


def gnews_rss(consulta: str) -> str:
    return "https://news.google.com/rss/search?" + up.urlencode(
        {"q": consulta, "hl": "es-419", "gl": "CL", "ceid": "CL:es-419"})


def leer_feed(fuente: dict, session: requests.Session) -> list:
    """Lee el feed principal más las consultas extra de config.consultas.

    Google News indexa el diario a medias y cada consulta devuelve un
    subconjunto distinto: medido el 13-10-2026, la consulta combinada trae 13
    avisos y "site:diarioavisoslegales.cl ejército" trae 11, con sólo 7 en
    común. Por eso una fuente puede tener varias consultas.
    """
    cfg = fuente.get("config") or {}
    items = _items(fuente["landing_url"], session)
    for consulta in cfg.get("consultas") or []:
        try:
            items += _items(gnews_rss(consulta), session)
        except Exception as e:
            log.warning("   consulta extra falló (%s): %s", consulta, e)

    corte = datetime.now(timezone.utc) - timedelta(days=int(cfg.get("dias_atras", DIAS_ATRAS)))
    dominios = set(cfg.get("dominios") or [])
    requiere = re.compile(cfg["requiere"], re.I) if cfg.get("requiere") else None
    avisos, vistos, links = [], set(), set()

    for item in items:
        titulo = " ".join((item.findtext("title") or "").split())
        link = (item.findtext("link") or "").strip()
        medio = (item.findtext("source") or "").strip()
        if not titulo or not link:
            continue
        if not medio and " - " in titulo:
            titulo, medio = titulo.rsplit(" - ", 1)
        elif medio and titulo.endswith(f" - {medio}"):
            titulo = titulo[: -len(medio) - 3]

        if PATRON_RUIDO.search(titulo) or not PATRON_DEFENSA.search(titulo):
            continue
        if requiere and not requiere.search(titulo):
            continue
        if link in links:
            continue
        links.add(link)

        pub = None
        try:
            pub = parsedate_to_datetime(item.findtext("pubDate"))
            pub = pub if pub.tzinfo else pub.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        if pub and pub < corte:
            continue

        real = decodificar_gnews(link, session)
        if not real:
            log.warning("   sin URL real, se omite: %s", titulo[:70])
            continue
        real = normalizar_url(real)
        dominio = up.urlsplit(real).netloc.replace("www.", "")
        if dominios and dominio not in dominios:
            continue
        if up.urlsplit(real).path in ("", "/"):
            continue   # la portada del diario se cuela en los resultados
        if real in vistos:
            continue
        vistos.add(real)

        avisos.append({"url": real, "gnews": link, "titulo": titulo[:400],
                       "medio": medio[:200] or dominio, "fecha": pub.date() if pub else None})
    return avisos


# ── Lectura del aviso ───────────────────────────────────────────────────────

def _texto_pdf(contenido: bytes) -> str:
    import pymupdf
    with pymupdf.open(stream=contenido, filetype="pdf") as doc:
        return "\n".join(p.get_text() for p in doc)


def leer_directo(url: str, session: requests.Session) -> str | None:
    """Intenta leer el aviso sin intermediarios. Devuelve texto o None.

    Hoy diarioavisoslegales.cl responde 403 (Cloudflare) a todo cliente sin
    JavaScript, así que esto casi siempre devuelve None. Se deja porque otros
    medios de la red ancha sí responden, y porque es gratis probar.
    """
    try:
        r = session.get(url, headers=HEADERS, timeout=40)
        if r.status_code != 200:
            log.info("   lectura directa: HTTP %s", r.status_code)
            return None
        tipo = r.headers.get("content-type", "")
        if "pdf" in tipo or url.lower().endswith(".pdf"):
            texto = _texto_pdf(r.content)
        else:
            sopa = BeautifulSoup(r.text, "html.parser")
            pdfs = [up.urljoin(url, a["href"]) for a in sopa.find_all("a", href=True)
                    if ".pdf" in a["href"].lower().split("?")[0]]
            partes = [" ".join(sopa.get_text(" ", strip=True).split())[:6000]]
            for pdf in pdfs[:2]:
                rp = session.get(pdf, headers=HEADERS, timeout=60)
                if rp.status_code == 200:
                    partes.append(f"--- PDF {pdf} ---\n" + _texto_pdf(rp.content))
            texto = "\n\n".join(partes)
        return texto if len(texto.strip()) >= MIN_CHARS_TEXTO else None
    except Exception as e:
        log.info("   lectura directa falló: %s", e)
        return None


SYSTEM = """Eres analista de inteligencia comercial de Bertonati Vehiculos Especiales,
fabricante chileno de ambulancias, carros bomba, vehiculos blindados y carrozados
especiales (clinicas moviles, oficinas moviles, puestos de mando, vehiculos
adaptados).

Lees avisos legales de compras de las Fuerzas Armadas de Chile (Ejercito, Armada,
FACh) y de Gendarmeria que NO pasan por Mercado Publico. Casi siempre son una tabla con varias
licitaciones: numero, rubro u objeto, correo para pedir bases y fecha de cierre
del proceso de solicitud. Algunos son precalificaciones de obras con venta de
bases, visita a terreno y apertura.

Registra CADA fila de la tabla como una licitacion. Copia el numero, el correo y
las fechas exactamente como aparecen. Las fechas vienen como "14-oct-26": devuelvelas
en formato AAAA-MM-DD. Si dos filas tienen el mismo numero, registra ambas.

ENCAJE con Bertonati:
- alto: ambulancias (incluida la militar o de campaña), vehiculos blindados, carros
  celulares o de traslado de internos,
  carrozados o vehiculos especiales, clinicas u hospitales de campaña moviles,
  shelters o modulos habilitados sobre vehiculo, puestos de mando o comunicaciones
  moviles, vehiculos de rescate o de bomberos, conversion o habilitacion de vehiculos.
- medio: adquisicion de vehiculos en general (transporte de personal, camiones,
  furgones, remolques), equipamiento medico para hospitales militares, carros de
  arrastre, contenedores habilitados.
- bajo: todo lo demas (vestuario, botas, monturas, combustible, repuestos y
  neumaticos, mantencion de aeronaves o simuladores, obras civiles, software,
  municiones, alimentacion).

categoria: ambulancia, blindado, rescate, carro_bomba o carroceria cuando corresponda;
otro en cualquier otro caso (incluidos vehiculos sin carrozar).

Si el aviso no es un llamado (por ejemplo una invitacion a inscribirse en el
registro de proveedores), registra tipo_aviso correspondiente y licitaciones vacia.

Si no pudiste leer el detalle (la pagina vino vacia, el PDF no cargo), pon
lectura_ok=false y no inventes filas."""

HERRAMIENTA = {
    "name": "registrar_aviso",
    "description": "Registra el contenido estructurado de un aviso de compras de Defensa.",
    "input_schema": {
        "type": "object",
        "properties": {
            "lectura_ok": {"type": "boolean",
                           "description": "true si se leyo el detalle del aviso (tabla o texto completo)"},
            "tipo_aviso": {"type": "string",
                           "enum": ["llamado_licitacion", "precalificacion", "registro_proveedores",
                                    "nota_prensa", "otro"]},
            "organismo": {"type": "string", "enum": ["ejercito", "armada", "fach", "gendarmeria", "otro"]},
            "unidad": {"type": "string", "description": "Ej. Division de Adquisiciones del Ejercito"},
            "fecha_publicacion": {"type": ["string", "null"], "description": "AAAA-MM-DD"},
            "requisitos": {"type": ["string", "null"],
                           "description": "Registro exigido (REPE, Registro de Contratistas FF.AA., etc.)"},
            "procedimiento": {"type": ["string", "null"],
                              "description": "Como se piden o compran las bases, en una frase"},
            "adjuntos": {"type": "array", "items": {"type": "string"},
                         "description": "URLs completas de los PDF adjuntos que aparecen en la pagina "
                                        "del aviso (/wp-content/uploads/...pdf). Llenalo siempre que la "
                                        "pagina los muestre, aunque no hayas podido abrirlos."},
            "licitaciones": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "numero": {"type": ["string", "null"]},
                        "objeto": {"type": "string"},
                        "correo": {"type": ["string", "null"]},
                        "fecha_limite": {"type": ["string", "null"], "description": "AAAA-MM-DD"},
                        "tipo_fecha_limite": {"type": "string",
                                              "enum": ["solicitud_bases", "venta_bases",
                                                       "presentacion_ofertas", "apertura", "otra"]},
                        "presupuesto_clp": {"type": ["number", "null"]},
                        "unidades": {"type": ["integer", "null"]},
                        "encaje": {"type": "string", "enum": ["alto", "medio", "bajo"]},
                        "categoria": {"type": "string",
                                      "enum": ["ambulancia", "carro_bomba", "blindado", "rescate",
                                               "carroceria", "otro"]},
                        "motivo_encaje": {"type": "string", "description": "Una frase"},
                    },
                    "required": ["objeto", "encaje", "categoria", "tipo_fecha_limite"],
                },
            },
        },
        "required": ["lectura_ok", "tipo_aviso", "organismo", "licitaciones"],
    },
}


def _llamar_api(cuerpo: dict, beta: str | None) -> dict:
    cab = {"x-api-key": _exigir_api_key(), "anthropic-version": "2023-06-01",
           "content-type": "application/json"}
    if beta:
        cab["anthropic-beta"] = beta
    for intento in range(4):
        r = requests.post(API, headers=cab, json=cuerpo, timeout=240)
        if r.status_code == 429 or r.status_code >= 500:
            espera = int(r.headers.get("retry-after", 2 ** intento * 5))
            log.warning("HTTP %s — esperando %ss", r.status_code, espera)
            time.sleep(espera)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"API {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("La API de Anthropic no respondió tras 4 intentos")


def extraer(aviso: dict, texto: str | None) -> tuple[dict | None, dict, str]:
    """Devuelve (registro, uso, metodo). registro None si no hubo tool_use.

    Con texto: una llamada simple. Sin texto: el modelo lee la URL con
    web_fetch (página del aviso y luego el PDF adjunto, que aparece como link
    en la página). web_fetch sólo puede abrir URLs que ya estén en la
    conversación, por eso va la URL en el mensaje.
    """
    if texto:
        metodo = "directo"
        contenido = (f"Aviso: {aviso['titulo']}\nURL: {aviso['url']}\n\n"
                     f"Texto extraido:\n{texto[:60000]}\n\nRegistra el aviso con registrar_aviso.")
        tools, beta = [HERRAMIENTA], None
    else:
        metodo = "web_fetch"
        dominio = up.urlsplit(aviso["url"]).netloc
        contenido = (f"Aviso: {aviso['titulo']}\nURL: {aviso['url']}\n\n"
                     "Abre la URL con web_fetch. Si es la pagina del aviso y el detalle esta en "
                     "un PDF adjunto (link a /wp-content/uploads/...pdf), abre tambien ese PDF. "
                     "Luego registra el aviso con registrar_aviso, y pon en adjuntos los links a "
                     "PDF que viste en la pagina.")
        fetch = {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 3,
                 "max_content_tokens": 40000}
        if dominio:
            fetch["allowed_domains"] = [dominio]
        tools, beta = [fetch, HERRAMIENTA], BETA_WEB_FETCH

    mensajes = [{"role": "user", "content": contenido}]
    uso: dict = {}
    for _ in range(4):
        data = _llamar_api({
            "model": MODELO, "max_tokens": 4096,
            "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            "tools": tools, "messages": mensajes,
            **({"tool_choice": {"type": "tool", "name": "registrar_aviso"}} if texto else {}),
        }, beta)
        for k, v in (data.get("usage") or {}).items():
            if isinstance(v, int):
                uso[k] = uso.get(k, 0) + v
        for b in data.get("content", []):
            if b.get("type") == "tool_use" and b.get("name") == "registrar_aviso":
                return b["input"], uso, metodo
        if data.get("stop_reason") == "pause_turn":
            # Turno largo con herramientas del servidor: se devuelve tal cual
            # para que el modelo siga donde quedó.
            mensajes.append({"role": "assistant", "content": data["content"]})
            continue
        if data.get("stop_reason") == "end_turn":
            # Leyó pero contestó en texto: se le pide el registro.
            mensajes.append({"role": "assistant", "content": data["content"]})
            mensajes.append({"role": "user", "content": "Registra ahora el aviso con registrar_aviso."})
            continue
        break
    return None, uso, metodo


# ── Persistencia ────────────────────────────────────────────────────────────

def _organismos() -> dict:
    global _ORG_CACHE
    if _ORG_CACHE is None:
        filas = sb.select("core_organismos", {"select": "id,nombre", "tipo": "eq.fuerzas_armadas"})
        por_nombre = {f["nombre"]: f["id"] for f in filas}
        _ORG_CACHE = {k: por_nombre.get(v) for k, v in ORGANISMOS.items()}
    return _ORG_CACHE


def _org_desde_titulo(titulo: str) -> str | None:
    t = _norm(titulo)
    if "ejercito" in t or "divae" in t: return "ejercito"
    if "armada" in t:                   return "armada"
    if "fuerza aerea" in t or re.search(r"\bfach\b", t): return "fach"
    if "gendarmeria" in t:              return "gendarmeria"
    return None


def _fecha(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def guardar_documento(aviso: dict, fuente: dict, org: str | None, cambios: dict) -> int | None:
    ahora = datetime.now(timezone.utc).isoformat()
    fila = {
        "source_id": fuente["id"], "organismo_id": _organismos().get(org),
        "title": aviso["titulo"], "autor_medio": aviso.get("medio") or None,
        "act_date": aviso["fecha"].isoformat() if aviso.get("fecha") else None,
        "published_date": aviso["fecha"].isoformat() if aviso.get("fecha") else None,
        "document_url": aviso["url"], "landing_url": aviso.get("gnews") or aviso["url"],
        "content_type": "application/pdf" if aviso["url"].lower().endswith(".pdf") else "text/html",
        "status": "discovered", "discovered_at": ahora,
    }
    filas = sb.insert(T_DOC, [fila], on_conflict="document_url", ignorar_dup=True)
    if filas:
        doc_id = filas[0]["id"]
    else:
        ex = sb.select(T_DOC, {"select": "id", "document_url": f"eq.{aviso['url']}", "limit": "1"})
        doc_id = ex[0]["id"] if ex else None
    if doc_id and cambios:
        sb.update(T_DOC, {"id": f"eq.{doc_id}"}, cambios)
    return doc_id


def construir_senal(lic: dict, reg: dict, aviso: dict, doc_id, fuente: dict) -> dict:
    org = reg.get("organismo") if reg.get("organismo") in CORTO else _org_desde_titulo(aviso["titulo"])
    org_id = _organismos().get(org) if org else None
    numero = (lic.get("numero") or "").strip() or None
    correo = (lic.get("correo") or "").strip().lower() or None
    limite = _fecha(lic.get("fecha_limite"))
    publicado = _fecha(reg.get("fecha_publicacion")) or aviso.get("fecha") or _hoy_cl()

    encaje = lic.get("encaje", "medio")
    prioridad = PRIORIDAD_POR_ENCAJE.get(encaje, 3)
    vencida = bool(limite and limite < _hoy_cl())
    if vencida:
        prioridad = 5            # se registra para el histórico, pero no va al correo
    confianza = 0.95 if reg.get("lectura_ok") else 0.5
    if vencida:
        confianza = min(confianza, 0.3)

    # La clave usa 'defensa' fijo en vez de la categoría: si el mismo llamado
    # se republica y el modelo lo categoriza distinto, igual debe ser uno solo.
    # Número + correo, no sólo número: el 07-2026 de DIVMANT y el 07-2026 de
    # Aviación son licitaciones distintas.
    codigo = f"{numero or (lic.get('objeto') or '')[:60]}|{correo or reg.get('unidad') or ''}"
    dedupe = sb.rpc("fn_core_dedupe_key", {
        "p_organismo_id": org_id, "p_categoria": "defensa", "p_etapa": "licitacion_publicada",
        "p_codigo": codigo, "p_fecha": publicado.isoformat(), "p_monto": None})
    score = sb.rpc("fn_core_score", {
        "p_etapa": "licitacion_publicada", "p_unidades": lic.get("unidades"),
        "p_monto_clp": lic.get("presupuesto_clp"), "p_prioridad": prioridad,
        "p_confianza": confianza, "p_fecha_evento": publicado.isoformat()})

    corto = CORTO.get(org, "Defensa")
    objeto = " ".join((lic.get("objeto") or "").split())
    titulo = f"{corto} · {numero} — {objeto}" if numero else f"{corto} — {objeto}"

    tipo_fecha = {"solicitud_bases": "pedir bases", "venta_bases": "comprar bases",
                  "presentacion_ofertas": "presentar ofertas", "apertura": "apertura"
                  }.get(lic.get("tipo_fecha_limite"), "plazo")
    partes = []
    if lic.get("motivo_encaje"):
        partes.append(f"Encaje {encaje}: {lic['motivo_encaje'].rstrip('.')}.")
    if limite:
        partes.append(f"Plazo para {tipo_fecha}: {limite.strftime('%d-%m-%Y')}.")
    if correo:
        partes.append(f"Bases a {correo}" + (f' con asunto "Solicita bases de licitación {numero}".'
                                             if numero and org == "ejercito" else "."))
    if reg.get("requisitos"):
        partes.append(f"Requisito: {reg['requisitos'].rstrip('.')}.")
    resumen = objeto
    if reg.get("unidad"):
        resumen += f" · {reg['unidad']}"
    if lic.get("presupuesto_clp"):
        resumen += f" · presupuesto estimado ${lic['presupuesto_clp']:,.0f}".replace(",", ".")

    return {
        "document_id": doc_id, "source_id": fuente["id"], "organismo_id": org_id,
        "fuente_tipo": "aviso_legal", "fuente_url": aviso["url"],
        "categoria": lic.get("categoria") or "otro", "etapa": "licitacion_publicada",
        "titulo": titulo[:500], "resumen": resumen[:2000],
        "por_que_importa": " ".join(partes)[:2000] or None,
        "unidades": lic.get("unidades"), "monto_clp": lic.get("presupuesto_clp"),
        "acuerdo_numero": numero, "fecha_evento": publicado.isoformat(),
        "fecha_limite": limite.isoformat() if limite else None, "contacto": correo,
        "confianza": confianza, "score": score, "dedupe_key": dedupe,
        "clasificador_modelo": MODELO, "prompt_version": PROMPT_VERSION,
        "clasificado_at": datetime.now(timezone.utc).isoformat(),
    }


def guardar_senal(fila: dict) -> str:
    """Inserta; si ya existía (republicación) sólo corre la fecha límite
    cuando el aviso nuevo trae una prórroga. Devuelve 'nueva'|'prorroga'|'repetida'."""
    if sb.insert(T_SEN, [fila], on_conflict="dedupe_key", ignorar_dup=True):
        return "nueva"
    ex = sb.select(T_SEN, {"select": "id,fecha_limite", "dedupe_key": f"eq.{fila['dedupe_key']}"})
    if ex and fila.get("fecha_limite") and (ex[0]["fecha_limite"] or "") < fila["fecha_limite"]:
        sb.update(T_SEN, {"id": f"eq.{ex[0]['id']}"}, {"fecha_limite": fila["fecha_limite"]})
        return "prorroga"
    return "repetida"


def senal_pendiente(aviso: dict, doc_id, fuente: dict) -> dict:
    """Señal mínima cuando el aviso no se pudo leer: que alguien lo abra."""
    org = _org_desde_titulo(aviso["titulo"])
    org_id = _organismos().get(org) if org else None
    fecha = (aviso.get("fecha") or _hoy_cl()).isoformat()
    clave = "url:" + hashlib.md5(aviso["url"].encode()).hexdigest()
    return {
        "document_id": doc_id, "source_id": fuente["id"], "organismo_id": org_id,
        "fuente_tipo": "aviso_legal", "fuente_url": aviso["url"],
        "categoria": "otro", "etapa": "licitacion_publicada",
        "titulo": (f"{CORTO.get(org, 'Defensa')} — aviso"
                   + (f" del {aviso['fecha'].strftime('%d-%m-%Y')}" if aviso.get("fecha") else "")
                   + " sin leer (no se pudo leer el detalle): abrir para ver rubros y plazos"),
        "por_que_importa": "Llamado de Defensa fuera de Mercado Público. El radar no pudo leer la tabla "
                           "de licitaciones; abre el aviso para ver rubros y plazos.",
        "fecha_evento": fecha, "confianza": 0.5,
        "score": sb.rpc("fn_core_score", {"p_etapa": "licitacion_publicada", "p_prioridad": 2,
                                          "p_confianza": 0.5, "p_fecha_evento": fecha}),
        "dedupe_key": sb.rpc("fn_core_dedupe_key", {
            "p_organismo_id": org_id, "p_categoria": "defensa", "p_etapa": "licitacion_publicada",
            "p_codigo": clave, "p_fecha": fecha, "p_monto": None}),
        "clasificador_modelo": MODELO, "prompt_version": PROMPT_VERSION,
        "clasificado_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Orquestación ────────────────────────────────────────────────────────────

def _leido(reg: dict | None) -> bool:
    return bool(reg and reg.get("lectura_ok") and reg.get("licitaciones") is not None)


def extraer_aviso(aviso: dict, texto: str | None) -> tuple[dict | None, dict, str]:
    """extraer() + segundo paso por el PDF adjunto.

    Primera corrida real (13-10-2026, dry-run en Actions): web_fetch lee bien
    los PDF directos, pero en las páginas /avisos/ el modelo veía el título y
    la fecha y no abría el PDF adjunto (lectura_ok=false). Ahora la página
    devuelve los links a PDF en `adjuntos` y el script los lee en una llamada
    aparte, con la URL del PDF en el mensaje. La fecha de publicación se toma
    de la página: los PDF no la traen.
    """
    reg, uso, metodo = extraer(aviso, texto)
    if _leido(reg) or aviso["url"].lower().endswith(".pdf") or not reg:
        return reg, uso, metodo
    pdfs = [normalizar_url(u) for u in (reg.get("adjuntos") or [])
            if ".pdf" in u.lower().split("?")[0] and u.startswith("http")]
    for pdf in list(dict.fromkeys(pdfs))[:2]:
        log.info("     leyendo PDF adjunto: %s", pdf)
        reg2, uso2, _ = extraer({**aviso, "url": pdf}, None)
        for k, v in uso2.items():
            uso[k] = uso.get(k, 0) + v
        if _leido(reg2):
            if not reg2.get("fecha_publicacion") and reg.get("fecha_publicacion"):
                reg2["fecha_publicacion"] = reg["fecha_publicacion"]
            reg2["adjuntos"] = pdfs
            return reg2, uso, "web_fetch_pdf"
    return reg, uso, metodo


def procesar_aviso(aviso: dict, fuente: dict, session: requests.Session, dry_run: bool) -> tuple[int, dict]:
    log.info("   · %s", aviso["url"])
    texto = leer_directo(aviso["url"], session)
    reg, uso, metodo = extraer_aviso(aviso, texto)
    ahora = datetime.now(timezone.utc).isoformat()
    org = (reg or {}).get("organismo") if (reg or {}).get("organismo") in CORTO \
        else _org_desde_titulo(aviso["titulo"])

    if dry_run:
        log.info("     [dry-run] %s", json.dumps(reg, ensure_ascii=False)[:1500])
        return len((reg or {}).get("licitaciones") or []), uso

    leido = _leido(reg)
    resumen_doc = json.dumps(reg, ensure_ascii=False) if reg else None
    cambios = {"extraction_method": metodo, "extracted_text": (texto or resumen_doc or "")[:200000] or None,
               "tokens_in": uso.get("input_tokens"), "tokens_out": uso.get("output_tokens"),
               "clasificador_modelo": MODELO, "prompt_version": PROMPT_VERSION, "parsed_at": ahora}
    if leido:
        es_llamado = reg.get("tipo_aviso") in ("llamado_licitacion", "precalificacion", "nota_prensa")
        cambios.update({"status": "clasificado" if es_llamado else "descartado",
                        "clasificado_at": ahora, "last_error": None,
                        "prefiltro_keywords": [reg.get("tipo_aviso")]})
    else:
        cambios.update({"status": "error",
                        "last_error": "no se pudo leer el detalle del aviso" + (f" ({metodo})" if metodo else "")})
    doc_id = guardar_documento(aviso, fuente, org, cambios)

    if not leido:
        # Sólo una señal pendiente por documento, aunque se reintente varios días.
        # La red ancha trae notas de prensa sobre licitaciones militares: si una
        # no se puede leer no vale la pena avisar (config.senal_si_falla=false).
        if (fuente.get("config") or {}).get("senal_si_falla") is False:
            return 0, uso
        ya = sb.select(T_SEN, {"select": "id", "document_id": f"eq.{doc_id}", "limit": "1"}) if doc_id else []
        if not ya:
            fila = senal_pendiente(aviso, doc_id, fuente)
            sb.insert(T_SEN, [fila], on_conflict="dedupe_key", ignorar_dup=True)
            log.warning("     no se pudo leer: queda señal pendiente (score %s)", fila["score"])
        return 0, uso

    # Si había una señal pendiente de una corrida anterior, la reemplaza el detalle.
    if doc_id:
        sb.update(T_SEN, {"document_id": f"eq.{doc_id}", "categoria": "eq.otro",
                          "titulo": "like.*no se pudo leer el detalle*",
                          "estado_gestion": "eq.nueva"},
                  {"estado_gestion": "descartada", "motivo_descarte": "reemplazada por el detalle del aviso"})

    n = 0
    for lic in reg.get("licitaciones") or []:
        fila = construir_senal(lic, reg, aviso, doc_id, fuente)
        estado = guardar_senal(fila)
        log.info("     → [%3d] %-8s %s%s", fila["score"], estado, fila["titulo"][:90],
                 f" · cierra {fila['fecha_limite']}" if fila["fecha_limite"] else "")
        n += estado == "nueva"
    return n, uso


def reintentos(fuentes_ids: list) -> list:
    """Avisos que fallaron hace poco: Cloudflare o web_fetch a veces fallan un día y pasan al otro."""
    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS_REINTENTO)).isoformat()
    filas = sb.select(T_DOC, {
        "select": "id,source_id,title,document_url,landing_url,published_date,autor_medio",
        "source_id": f"in.({','.join(map(str, fuentes_ids))})",
        "status": "in.(error,discovered)", "discovered_at": f"gte.{desde}"})
    return [{"source_id": f["source_id"], "url": f["document_url"], "gnews": f["landing_url"],
             "titulo": f["title"], "medio": f["autor_medio"],
             "fecha": _fecha(f["published_date"])} for f in filas]


def main() -> int:
    ap = argparse.ArgumentParser(description="Llamados de Defensa fuera de Mercado Público")
    ap.add_argument("--slug", help="procesar solo esta fuente")
    ap.add_argument("--url", help="procesar un aviso puntual (URL del aviso o del PDF)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    params = {"select": "id,slug,nombre,landing_url,config,rate_limit_ms,consecutive_errors",
              "fuente_tipo": "eq.aviso_legal", "enabled": "is.true", "order": "slug.asc"}
    if args.slug:
        params["slug"] = f"eq.{args.slug}"
    fuentes = sb.select("core_sources", params)
    if not fuentes:
        log.error("No hay fuentes aviso_legal habilitadas.")
        return 0
    por_id = {f["id"]: f for f in fuentes}
    log.info("Fuentes de avisos de Defensa: %d", len(fuentes))

    t0 = time.time()
    nuevas = leidos = fallidas = 0
    tok_in = tok_out = 0

    with requests.Session() as session:
        if args.url:
            principal = next((f for f in fuentes if "diarioavisoslegales" in f["slug"]), fuentes[0])
            cola = [(principal, {"url": normalizar_url(args.url), "gnews": None, "medio": None,
                                 "titulo": args.url.rsplit("/", 1)[-1], "fecha": None})]
        else:
            vistos = set() if args.dry_run else {
                d["document_url"] for d in sb.select(T_DOC, {
                    "select": "document_url",
                    "source_id": f"in.({','.join(str(f['id']) for f in fuentes)})"})}
            cola = []
            for f in fuentes:
                try:
                    avisos = leer_feed(f, session)
                    nuevos = [a for a in avisos if a["url"] not in vistos]
                    vistos |= {a["url"] for a in nuevos}
                    log.info("[%s] %d avisos de Defensa en el feed, %d nuevos",
                             f["slug"], len(avisos), len(nuevos))
                    cola += [(f, a) for a in nuevos]
                    if not args.dry_run:
                        sb.marcar_fuente_ok(f["id"], len(nuevos))
                except Exception as e:
                    fallidas += 1
                    log.error("   ERROR de fuente %s: %s", f["slug"], e)
                    if not args.dry_run:
                        sb.marcar_fuente_error(f["id"], f.get("consecutive_errors"), str(e))
                time.sleep((f.get("rate_limit_ms") or 3000) / 1000)
            if not args.dry_run:
                previos = [(por_id[a["source_id"]], a) for a in reintentos(list(por_id))
                           if a["url"] not in {x[1]["url"] for x in cola}]
                if previos:
                    log.info("Reintentos de avisos sin leer: %d", len(previos))
                cola += previos

        for fuente, aviso in cola[:MAX_POR_CORRIDA]:
            try:
                n, uso = procesar_aviso(aviso, fuente, session, args.dry_run)
                nuevas += n
                leidos += 1
                tok_in += uso.get("input_tokens", 0)
                tok_out += uso.get("output_tokens", 0)
            except Exception as e:
                log.error("   ERROR leyendo %s: %s", aviso["url"], e)
        if len(cola) > MAX_POR_CORRIDA:
            log.info("Quedan %d avisos para la próxima corrida", len(cola) - MAX_POR_CORRIDA)

    log.info("Avisos leídos: %d | señales nuevas: %d | fuentes con error: %d | "
             "tokens in/out: %s/%s | %.1fs", leidos, nuevas, fallidas,
             f"{tok_in:,}", f"{tok_out:,}", time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
