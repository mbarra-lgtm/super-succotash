"""
scrape_core.py
==============
Descubre y baja actas/acuerdos nuevos de los Consejos Regionales (CORE),
extrae el texto (con OCR cuando el PDF viene escaneado) y los deja en
core_documents listos para el clasificador.

Programar: 2 veces al día. La frecuencia real la decide cada fuente vía
core_sources.frecuencia_horas — las regiones prioritarias van cada 12h.

    python scrape_core.py                      # fuentes cuya frecuencia venció
    python scrape_core.py --slug gore-biobio-acuerdos
    python scrape_core.py --dry-run            # no escribe en Supabase

Dos cosas que enseñó el piloto y están codificadas acá:
  * Bajar todo PDF que aparezca trae basura: el piloto guardó un
    "Instructivo FIC-R 2021" y un "Horario de atención". Se filtra por
    patrón de nombre y por año ANTES de descargar.
  * Atacama y O'Higgins publican PDFs escaneados (17 chars/página). Sin OCR
    esas regiones no producen nada y el sistema no avisa. Por eso un PDF
    escaneado sin OCR queda status='error', nunca 'parsed' con texto vacío.
"""

import os, re, io, sys, time, hashlib, logging, argparse, tempfile, subprocess
import urllib.parse as up
import urllib.robotparser as robotparser
from datetime import date, datetime, timezone

import requests
import pymupdf   # el import `fitz` está deprecado desde PyMuPDF 1.24
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
log = logging.getLogger("scrape_core")

# ── Config ──────────────────────────────────────────────────────────────────
UA      = "BertonatiRadar/1.0 (+https://bertonati.cl; monitoreo de acuerdos publicos)"
HEADERS = {"User-Agent": UA}

MIN_CHARS_POR_PAGINA = int(os.getenv("CORE_MIN_CHARS_PAGINA", "200"))
MAX_MB               = int(os.getenv("CORE_MAX_MB", "40"))
MAX_POR_CORRIDA      = int(os.getenv("CORE_MAX_POR_CORRIDA", "15"))
OCR_HABILITADO       = os.getenv("CORE_OCR", "1") == "1"
ROBOTS_TIMEOUT       = int(os.getenv("CORE_ROBOTS_TIMEOUT", "10"))
LANDING_TIMEOUT      = int(os.getenv("CORE_LANDING_TIMEOUT", "25"))

T_DOC = "core_documents"

PATRON_RUIDO = re.compile(
    r"instructivo|horario|organigrama|formulario|manual|bases\s+administrativas"
    r"|reglamento|circular\s+interna|convocatoria|llamado\s+a\s+concurso", re.I)
PATRON_UTIL = re.compile(r"acta|acuerdo|sesion|sesión|pleno|certificado|cert", re.I)

ANIOS_VIGENTES = {str(a) for a in range(date.today().year - 1, date.today().year + 1)}


# ── Descubrimiento ──────────────────────────────────────────────────────────

def _robots_permite(url: str) -> bool:
    """Consulta robots.txt con timeout propio.

    RobotFileParser.read() usa urlopen SIN timeout: si el host no responde, la
    corrida se cuelga ahí. En el dry-run del 31-08 la fuente de La Araucanía
    tardó ~3 minutos en fallar por esto. Se busca el robots con requests y
    recién después se parsea.
    """
    p = up.urlsplit(url)
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt",
                         headers=HEADERS, timeout=ROBOTS_TIMEOUT)
        # RFC 9309: 401 y 403 significan "prohibido completo"; el resto de los
        # 4xx significan "sin restricciones". La primera versión de este fix
        # trataba todo 4xx como permitido, que es más laxo que la norma.
        if r.status_code in (401, 403):
            return False
        if r.status_code >= 400:
            return True   # sin robots.txt: permitido
        rp = robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(UA, url)
    except Exception as e:
        log.debug("robots.txt de %s no legible (%s): se asume permitido", p.netloc, e)
        return True  # es info pública y vamos a ritmo lento igual


def _fecha_desde_texto(txt: str):
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", txt)
    if m:
        d, mes, a = (int(x) for x in m.groups())
        try: return date(a, mes, d)
        except ValueError: return None
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", txt)
    if m:
        a, mes, d = (int(x) for x in m.groups())
        try: return date(a, mes, d)
        except ValueError: return None
    return None


def descubrir(fuente: dict, session: requests.Session) -> list:
    """Enlaces a PDF de la landing, filtrados. Los parsers html_list /
    search_page / custom comparten esta implementación; cuando una fuente
    necesite lógica propia se agrega acá por slug."""
    cfg     = fuente.get("config") or {}
    landing = fuente["landing_url"]

    r = session.get(landing, headers=HEADERS, timeout=LANDING_TIMEOUT)
    r.raise_for_status()
    sopa = BeautifulSoup(r.text, "html.parser")
    raiz = sopa.select_one(cfg["selector_contenedor"]) if cfg.get("selector_contenedor") else None
    raiz = raiz or sopa

    candidatos = {}
    for a in raiz.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "javascript:", "#")):
            continue
        url = up.urljoin(landing, href)
        if ".pdf" not in url.lower().split("?")[0]:
            continue

        titulo = " ".join(a.get_text(" ", strip=True).split())
        if len(titulo) < 8 or titulo.lower() in {"ver documento", "descargar", "pdf", "aqui", "aquí"}:
            titulo = up.unquote(url.rsplit("/", 1)[-1])

        ctx = f"{titulo} {url}"
        if PATRON_RUIDO.search(ctx):        continue
        if not PATRON_UTIL.search(ctx):     continue
        if not any(x in ctx for x in ANIOS_VIGENTES): continue

        candidatos[url] = {"url": url, "titulo": titulo[:400],
                           "fecha": _fecha_desde_texto(ctx)}

    orden = sorted(candidatos.values(), key=lambda c: (c["fecha"] or date.min), reverse=True)
    return orden[: int(cfg.get("max_por_corrida", MAX_POR_CORRIDA))]


# ── Descarga y extracción ───────────────────────────────────────────────────

def _ocr(pdf_bytes: bytes):
    if not OCR_HABILITADO:
        return None, None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as ent, \
             tempfile.NamedTemporaryFile(suffix=".pdf") as sal:
            ent.write(pdf_bytes); ent.flush()
            subprocess.run(["ocrmypdf", "--force-ocr", "--language", "spa",
                            "--optimize", "0", "--quiet", ent.name, sal.name],
                           check=True, timeout=600)
            with pymupdf.open(sal.name) as doc:
                return "\n".join(p.get_text() for p in doc), "ocrmypdf"
    except FileNotFoundError:
        log.warning("ocrmypdf no instalado — el documento quedará en error")
    except Exception as e:
        log.warning("OCR falló: %s", e)
    return None, None


def descargar_y_extraer(url: str, session: requests.Session) -> dict:
    r = session.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()
    contenido = r.content

    if len(contenido) > MAX_MB * 1024 * 1024:
        return {"status": "error", "last_error": f"PDF > {MAX_MB}MB"}

    sha = hashlib.sha256(contenido).hexdigest()
    try:
        with pymupdf.open(stream=io.BytesIO(contenido), filetype="pdf") as doc:
            paginas = doc.page_count
            texto   = "\n".join(p.get_text() for p in doc)
    except Exception as e:
        return {"status": "error", "file_sha256": sha, "last_error": f"PDF ilegible: {e}"}

    metodo     = "pymupdf"
    por_pagina = len(texto) / paginas if paginas else 0
    if por_pagina < MIN_CHARS_POR_PAGINA:
        texto_ocr, metodo_ocr = _ocr(contenido)
        if texto_ocr:
            texto, metodo = texto_ocr, metodo_ocr
        else:
            return {"status": "error", "file_sha256": sha, "page_count": paginas,
                    "content_type": r.headers.get("Content-Type"),
                    "last_error": f"PDF escaneado ({por_pagina:.0f} chars/pág) y sin OCR"}

    ahora = datetime.now(timezone.utc).isoformat()
    return {"status": "parsed", "file_sha256": sha, "page_count": paginas,
            "extracted_text": texto, "extraction_method": metodo,
            "content_type": r.headers.get("Content-Type"),
            "downloaded_at": ahora, "parsed_at": ahora}


# ── Prefiltro ───────────────────────────────────────────────────────────────

def evaluar_prefiltro(texto: str):
    """Pasa si hay keyword de peso alto (ambulancia, carro bomba, blindado,
    material mayor) o si la suma de pesos llega a 4.

    Medido contra las actas del piloto, esta regla deja pasar prácticamente
    toda acta real ("FNDR" aparece 9 veces en una sesión ordinaria). Es
    deliberado: el ahorro no está acá sino en los pasajes — un acta de
    177.000 caracteres se reduce a ~14 pasajes de 700 (94% menos texto) y
    clasificarla cuesta centavos.

    El gate NO debe endurecerse a punta de keywords. "Construcción Cuartel
    segunda compañía del Cuerpo de bomberos de Lota" tiene las mismas
    palabras que una compra de carro bomba y no es una oportunidad. Esa
    distinción la hace el modelo, no una lista de términos.
    """
    filas = sb.rpc("fn_core_prefiltro", {"p_texto": texto[:400_000], "p_ventana": 700}) or []
    positivas = [f for f in filas if not f["es_negativo"]]
    if any(f["es_negativo"] for f in filas) and not any(f["peso"] >= 3 for f in positivas):
        return False, []
    terminos = sorted({f["termino"] for f in positivas})
    if any(f["peso"] >= 3 for f in positivas):
        return True, terminos
    return sum(f["peso"] for f in positivas) >= 4, terminos


# ── Orquestación ────────────────────────────────────────────────────────────

def procesar_fuente(fuente: dict, session: requests.Session, dry_run: bool) -> int:
    log.info("[%s] %s", fuente["slug"], fuente["nombre"])
    if not _robots_permite(fuente["landing_url"]):
        raise RuntimeError("robots.txt no permite esta URL")

    vistas     = set() if dry_run else sb.urls_ya_vistas(fuente["id"])
    candidatos = [c for c in descubrir(fuente, session) if c["url"] not in vistas]
    log.info("   candidatos nuevos: %d", len(candidatos))

    pausa  = (fuente.get("rate_limit_ms") or 2000) / 1000
    nuevos = 0

    for c in candidatos:
        time.sleep(pausa)
        log.info("   - %s", c["titulo"][:70])
        try:
            extraido = descargar_y_extraer(c["url"], session)
        except Exception as e:
            extraido = {"status": "error", "last_error": str(e)[:400]}

        fila = {
            "source_id":    fuente["id"],
            "organismo_id": fuente.get("organismo_id"),
            "region":       fuente.get("region"),
            "comuna":       fuente.get("comuna"),
            "title":        c["titulo"],
            "act_date":     c["fecha"].isoformat() if c["fecha"] else None,
            "document_url": c["url"],
            "landing_url":  fuente["landing_url"],
            "file_name":    up.unquote(c["url"].rsplit("/", 1)[-1])[:300],
            **extraido,
        }

        if fila["status"] == "parsed":
            # El prefiltro SÍ corre en dry-run: es una llamada de solo lectura
            # (fn_core_prefiltro no escribe nada) y es justamente lo que uno
            # quiere ver en una corrida en seco. Antes se saltaba, y el log
            # reportaba "descartado por prefiltro" para TODO — un resultado
            # falso que hacía inútil el dry-run.
            paso, terminos = evaluar_prefiltro(fila["extracted_text"])
            fila.update({"prefiltro_ok": paso, "prefiltro_keywords": terminos,
                         "prefiltrado_at": datetime.now(timezone.utc).isoformat(),
                         "status": "prefiltrado"})
            log.info("     %sp, %s chars [%s] → %s %s",
                     fila["page_count"], f"{len(fila['extracted_text']):,}",
                     fila["extraction_method"],
                     "PASA al clasificador" if paso else "descartado por prefiltro",
                     terminos or "")
        else:
            log.warning("     ERROR: %s", fila.get("last_error"))

        if not dry_run:
            sb.insert(T_DOC, [fila], on_conflict="document_url", ignorar_dup=True)
        nuevos += 1

    return nuevos


def main() -> int:
    ap = argparse.ArgumentParser(description="Scraper de acuerdos CORE")
    ap.add_argument("--slug", help="procesar solo esta fuente")
    ap.add_argument("--dry-run", action="store_true", help="no escribe en Supabase")
    args = ap.parse_args()

    t0      = time.time()
    fuentes = sb.fuentes_pendientes(args.slug)
    log.info("Fuentes a revisar: %d", len(fuentes))

    total = fallidas = 0
    with requests.Session() as session:
        for f in fuentes:
            try:
                nuevos = procesar_fuente(f, session, args.dry_run)
                total += nuevos
                if not args.dry_run:
                    sb.marcar_fuente_ok(f["id"], nuevos)
            except Exception as e:
                fallidas += 1
                log.error("   ERROR de fuente %s: %s", f["slug"], e)
                if not args.dry_run:
                    sb.marcar_fuente_error(f["id"], f.get("consecutive_errors"), str(e))

    log.info("Documentos nuevos: %d | fuentes con error: %d | %.1fs",
             total, fallidas, time.time() - t0)
    if fallidas:
        log.warning("%d fuente(s) con error. El detalle vive en "
                    "v_core_fuentes_salud, no en el exit code.", fallidas)

    # Salida SIEMPRE 0 mientras el scraper haya podido correr.
    #
    # Antes esto devolvía 1 cuando fallaba más de la mitad de las fuentes, y el
    # 31-08 eso volteó el pipeline completo: 12 de 18 fuentes en error → exit 1
    # → GitHub Actions saltó los pasos siguientes → las 11 actas que SÍ se
    # habían bajado nunca se clasificaron.
    #
    # Que un sitio del Estado esté caído es el estado normal del mundo, no una
    # falla de este job. La salud por fuente ya se registra en core_sources
    # (consecutive_errors, last_ok_at) y se consulta en v_core_fuentes_salud;
    # el exit code no es el lugar para eso, porque acá significa "no sigas".
    return 0


if __name__ == "__main__":
    sys.exit(main())
