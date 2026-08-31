"""
ingesta_prensa.py
=================
Capa de prensa del radar: lee los feeds RSS de core_sources
(fuente_tipo='rss_prensa'), descarta la crónica policial con el diccionario
negativo y clasifica las notas sobrevivientes en lotes.

Programar: 1 vez al día, junto al scraper CORE.

    python ingesta_prensa.py                   # todas las fuentes de prensa
    python ingesta_prensa.py --slug prensa-ambulancias-compra
    python ingesta_prensa.py --dry-run         # no escribe en Supabase

Por qué es un script aparte y no un parser más de scrape_core.py:

* **El filtro se invierte.** En las actas el ruido es burocrático y las
  palabras del rubro casi no aparecen; en la prensa "ambulancia" es sobre todo
  crónica de accidentes. Acá una keyword negativa PESA MÁS que una positiva
  (ver `filtrar_nota`), al revés que en las actas.
* **Se clasifica en lote.** Una nota son ~300 caracteres. Mandar 40 llamadas
  separadas a la API sería absurdo; van de a `LOTE` en una sola.
* **No se guarda el cuerpo de la nota.** Solo título, bajada, medio, fecha y
  link. El RSS existe para ser consumido, pero el texto completo de una nota es
  contenido de terceros y el valor del radar es la señal + el link a la fuente,
  no ser un archivo de prensa.

La prensa dispara, el CORE confirma: una nota publica el acuerdo el mismo día
de la sesión, y el acta oficial llega 2 a 6 semanas después con el número de
acuerdo. `fn_core_dedupe_key` las une en una sola señal.
"""

import os, re, sys, time, logging, argparse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

import sb
from prompt_radar import SYSTEM, PROMPT_VERSION, herramienta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ingesta_prensa")

# ── Config ──────────────────────────────────────────────────────────────────
API           = "https://api.anthropic.com/v1/messages"
MODELO        = os.getenv("RADAR_MODELO", "claude-sonnet-4-5")
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

UA      = "BertonatiRadar/1.0 (+https://bertonati.cl; monitoreo de compras publicas)"
HEADERS = {"User-Agent": UA}

LOTE          = int(os.getenv("PRENSA_LOTE", "20"))       # notas por llamada
DIAS_ATRAS    = int(os.getenv("PRENSA_DIAS_ATRAS", "8"))  # ventana de publicación
MAX_POR_FUENTE = int(os.getenv("PRENSA_MAX_POR_FUENTE", "40"))

T_DOC = "core_documents"
T_SEN = "core_senales"

_KEYWORDS_CACHE: list | None = None


# ── Lectura del feed ────────────────────────────────────────────────────────

def _limpiar(html: str) -> str:
    if not html:
        return ""
    return " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split())


def leer_feed(fuente: dict, session: requests.Session) -> list:
    """Devuelve notas {url, titulo, snippet, medio, fecha} del RSS."""
    r = session.get(fuente["landing_url"], headers=HEADERS, timeout=45)
    r.raise_for_status()

    try:
        raiz = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise RuntimeError(f"RSS ilegible: {e}")

    corte = datetime.now(timezone.utc) - timedelta(days=DIAS_ATRAS)
    tope  = int((fuente.get("config") or {}).get("max_por_corrida", MAX_POR_FUENTE))
    notas = []

    for item in raiz.iter("item"):
        titulo = _limpiar((item.findtext("title") or ""))
        link   = (item.findtext("link") or "").strip()
        if not titulo or not link:
            continue

        pub = None
        if item.findtext("pubDate"):
            try:
                pub = parsedate_to_datetime(item.findtext("pubDate"))
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
            except Exception:
                pub = None
        if pub and pub < corte:
            continue

        medio = item.findtext("source") or ""
        # Google News agrega " - Medio" al final del titular; se separa para
        # que el clasificador no lo lea como parte del hecho.
        if not medio and " - " in titulo:
            titulo, medio = titulo.rsplit(" - ", 1)

        notas.append({
            "url": link,
            "titulo": titulo[:400],
            "snippet": _limpiar(item.findtext("description") or "")[:1200],
            "medio": _limpiar(medio)[:200],
            "fecha": pub.date() if pub else None,
        })
        if len(notas) >= tope:
            break

    return notas


# ── Filtro (aquí el negativo manda) ─────────────────────────────────────────

def _keywords() -> list:
    global _KEYWORDS_CACHE
    if _KEYWORDS_CACHE is None:
        _KEYWORDS_CACHE = sb.select("core_keywords", {
            "select": "termino,categoria,peso,es_negativo", "activa": "is.true"})
    return _KEYWORDS_CACHE


def _normalizar(t: str) -> str:
    return (t or "").lower().translate(str.maketrans("áéíóúüñ", "aeiouun"))


def filtrar_nota(nota: dict) -> tuple[bool, int, list]:
    """Puntaje = suma(pesos positivos) − 2 × suma(pesos negativos). Pasa si > 0.

    El factor 2 es deliberado y es la diferencia con el prefiltro de actas.
    "Un joven fue trasladado en ambulancia tras un accidente" contiene
    'ambulancia' (positiva, peso 3) y dispararía el gate de actas; acá suma
    3 − 2×(3+3) = −9 y se descarta sin gastar un token.

    Calibrarlo con core_senal_feedback: si aparecen falsos negativos, bajar el
    factor antes que borrar keywords negativas.
    """
    texto = _normalizar(f"{nota['titulo']} {nota['snippet']}")
    positivo = negativo = 0
    encontradas = []

    for k in _keywords():
        if _normalizar(k["termino"]) in texto:
            if k["es_negativo"]:
                negativo += k["peso"]
            else:
                positivo += k["peso"]
                encontradas.append(k["termino"])

    puntaje = positivo - 2 * negativo
    return puntaje > 0, puntaje, sorted(set(encontradas))


# ── Clasificación en lote ───────────────────────────────────────────────────

def clasificar_lote(notas: list) -> tuple[list, dict]:
    listado = []
    for i, n in enumerate(notas):
        listado.append(
            f"[{i}] {n['titulo']}\n"
            f"    medio: {n['medio'] or 'no informado'} | "
            f"fecha: {n['fecha'].isoformat() if n['fecha'] else 'no informada'}\n"
            f"    {n['snippet']}"
        )
    contenido = (
        "Notas de prensa chilena. Para cada una decide si es una señal comercial.\n"
        "Recuerda: la crónica de accidentes e incendios NO es una señal.\n"
        "Indica en indice_item el número entre corchetes del que sale cada señal.\n\n"
        + "\n\n".join(listado)
    )

    cuerpo = {
        "model": MODELO, "max_tokens": 8192,
        "system": [{"type": "text", "text": SYSTEM,
                    "cache_control": {"type": "ephemeral"}}],
        "tools": [herramienta(con_indice=True)],
        "tool_choice": {"type": "tool", "name": "registrar_senales"},
        "messages": [{"role": "user", "content": contenido}],
    }
    cabeceras = {"x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"}

    for intento in range(4):
        r = requests.post(API, headers=cabeceras, json=cuerpo, timeout=180)
        if r.status_code == 429 or r.status_code >= 500:
            espera = int(r.headers.get("retry-after", 2 ** intento * 5))
            log.warning("HTTP %s — esperando %ss", r.status_code, espera)
            time.sleep(espera)
            continue
        r.raise_for_status()
        data = r.json()
        for bloque in data.get("content", []):
            if bloque.get("type") == "tool_use":
                return bloque["input"].get("senales", []), data.get("usage", {})
        return [], data.get("usage", {})

    raise RuntimeError("La API de Anthropic no respondió tras 4 intentos")


# ── Persistencia ────────────────────────────────────────────────────────────

def guardar_documento(nota: dict, fuente: dict) -> int | None:
    """Guarda la nota como documento liviano: titulo + bajada, nunca el cuerpo."""
    ahora = datetime.now(timezone.utc).isoformat()
    fila = {
        "source_id": fuente["id"], "organismo_id": fuente.get("organismo_id"),
        "region": fuente.get("region"), "title": nota["titulo"],
        "autor_medio": nota["medio"] or None,
        "act_date": nota["fecha"].isoformat() if nota["fecha"] else None,
        "published_date": nota["fecha"].isoformat() if nota["fecha"] else None,
        "document_url": nota["url"], "landing_url": fuente["landing_url"],
        "extracted_text": f"{nota['titulo']}\n\n{nota['snippet']}",
        "extraction_method": "rss", "content_type": "text/html",
        "status": "prefiltrado", "prefiltro_ok": True,
        "prefiltro_keywords": nota["keywords"], "prefiltrado_at": ahora,
        "downloaded_at": ahora, "parsed_at": ahora,
    }
    filas = sb.insert(T_DOC, [fila], on_conflict="document_url", ignorar_dup=True)
    if filas:
        return filas[0]["id"]
    existente = sb.select(T_DOC, {"select": "id",
                                  "document_url": f"eq.{nota['url']}", "limit": "1"})
    return existente[0]["id"] if existente else None


def construir_senal(senal: dict, nota: dict, doc_id: int | None, fuente: dict) -> dict:
    fecha = senal.get("fecha_evento") or (
        nota["fecha"].isoformat() if nota["fecha"] else date.today().isoformat())
    codigo = senal.get("codigo_bip") or senal.get("acuerdo_numero")

    dedupe = sb.rpc("fn_core_dedupe_key", {
        "p_organismo_id": fuente.get("organismo_id"), "p_categoria": senal["categoria"],
        "p_etapa": senal["etapa"], "p_codigo": codigo,
        "p_fecha": fecha, "p_monto": senal.get("monto_clp")})
    score = sb.rpc("fn_core_score", {
        "p_etapa": senal["etapa"], "p_unidades": senal.get("unidades"),
        "p_monto_clp": senal.get("monto_clp"), "p_prioridad": 3,
        "p_confianza": senal.get("confianza", 0.5)})

    return {
        "document_id": doc_id, "source_id": fuente["id"],
        "organismo_id": fuente.get("organismo_id"),
        "fuente_tipo": "rss_prensa", "fuente_url": nota["url"],
        "region": senal.get("region"), "comuna": senal.get("comuna"),
        "categoria": senal["categoria"], "etapa": senal["etapa"],
        "titulo": senal["titulo"][:500], "resumen": senal.get("resumen"),
        "por_que_importa": senal.get("por_que_importa"),
        "unidades": senal.get("unidades"), "monto_clp": senal.get("monto_clp"),
        "monto_raw": senal.get("monto_raw"), "codigo_bip": senal.get("codigo_bip"),
        "acuerdo_numero": senal.get("acuerdo_numero"), "fecha_evento": fecha,
        "confianza": senal.get("confianza"), "score": score, "dedupe_key": dedupe,
        "clasificador_modelo": MODELO, "prompt_version": PROMPT_VERSION,
        "clasificado_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Orquestación ────────────────────────────────────────────────────────────

def procesar_fuente(fuente: dict, session: requests.Session, dry_run: bool) -> tuple[int, int, dict]:
    log.info("[%s] %s", fuente["slug"], fuente["nombre"])
    notas = leer_feed(fuente, session)
    vistas = set() if dry_run else sb.urls_ya_vistas(fuente["id"])
    notas = [n for n in notas if n["url"] not in vistas]
    log.info("   notas nuevas en el feed: %d", len(notas))
    if not notas:
        return 0, 0, {}

    candidatas, descartadas = [], 0
    for n in notas:
        pasa, puntaje, kws = filtrar_nota(n)
        if pasa:
            n["keywords"] = kws
            candidatas.append(n)
        else:
            descartadas += 1
            log.debug("   descartada (%d): %s", puntaje, n["titulo"][:70])

    log.info("   pasan el filtro: %d | descartadas por ruido: %d",
             len(candidatas), descartadas)
    if not candidatas:
        return 0, descartadas, {}

    total_senales, uso_total = 0, {}
    for i in range(0, len(candidatas), LOTE):
        lote = candidatas[i:i + LOTE]
        senales, uso = clasificar_lote(lote)
        for k, v in uso.items():
            if isinstance(v, int):
                uso_total[k] = uso_total.get(k, 0) + v
        log.info("   lote de %d notas → %d señales", len(lote), len(senales))

        for s in senales:
            idx = s.get("indice_item")
            if idx is None or not (0 <= idx < len(lote)):
                log.warning("   señal con indice_item inválido (%s), se omite", idx)
                continue
            nota   = lote[idx]
            doc_id = None if dry_run else guardar_documento(nota, fuente)
            fila   = construir_senal(s, nota, doc_id, fuente)
            log.info("   → [%3d] %s/%s: %s", fila["score"], fila["categoria"],
                     fila["etapa"], fila["titulo"][:80])
            if not dry_run:
                sb.insert(T_SEN, [fila], on_conflict="dedupe_key", ignorar_dup=True)
            total_senales += 1

        # Las notas que no produjeron señal igual se guardan: evitan
        # reprocesarlas mañana y dejan medible la tasa de acierto del filtro.
        if not dry_run:
            for nota in lote:
                doc_id = guardar_documento(nota, fuente)
                if doc_id:
                    sb.update(T_DOC, {"id": f"eq.{doc_id}"}, {
                        "status": "clasificado",
                        "clasificado_at": datetime.now(timezone.utc).isoformat(),
                        "clasificador_modelo": MODELO, "prompt_version": PROMPT_VERSION})

    return total_senales, descartadas, uso_total


def main() -> int:
    ap = argparse.ArgumentParser(description="Capa de prensa del radar comercial")
    ap.add_argument("--slug", help="procesar solo esta fuente")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    params = {"select": "id,slug,nombre,region,comuna,organismo_id,fuente_tipo,"
                        "landing_url,config,rate_limit_ms,consecutive_errors",
              "fuente_tipo": "eq.rss_prensa", "enabled": "is.true",
              "order": "slug.asc"}
    if args.slug:
        params["slug"] = f"eq.{args.slug}"
    fuentes = sb.select("core_sources", params)
    log.info("Fuentes de prensa: %d", len(fuentes))

    t0 = time.time()
    senales = descartadas = fallidas = 0
    tok_in = tok_out = 0

    with requests.Session() as session:
        for f in fuentes:
            try:
                s, d, uso = procesar_fuente(f, session, args.dry_run)
                senales     += s
                descartadas += d
                tok_in      += uso.get("input_tokens", 0)
                tok_out     += uso.get("output_tokens", 0)
                if not args.dry_run:
                    sb.marcar_fuente_ok(f["id"], s)
                time.sleep((f.get("rate_limit_ms") or 3000) / 1000)
            except Exception as e:
                fallidas += 1
                log.error("   ERROR de fuente %s: %s", f["slug"], e)
                if not args.dry_run:
                    sb.marcar_fuente_error(f["id"], f.get("consecutive_errors"), str(e))

    log.info("Señales: %d | descartadas por ruido: %d | fuentes con error: %d | "
             "tokens in/out: %s/%s | %.1fs",
             senales, descartadas, fallidas, f"{tok_in:,}", f"{tok_out:,}",
             time.time() - t0)
    return 1 if fuentes and fallidas > len(fuentes) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
