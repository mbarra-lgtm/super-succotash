"""
clasificar_senales.py
=====================
Convierte pasajes de actas CORE en señales comerciales (core_senales):
categoría, etapa, unidades, monto, score y clave de deduplicación.

Programar: después de scrape_core.py, en la misma corrida.

    python clasificar_senales.py               # todo lo pendiente
    python clasificar_senales.py --doc 8       # un documento puntual
    python clasificar_senales.py --dry-run     # imprime, no escribe

Por qué existe esta capa: en el piloto, un acta del CORE del Biobío generó
12 filas en core_projects que eran EL MISMO hecho (el diseño de un cuartel
de bomberos en Lota) y además un hecho irrelevante — es un edificio, no un
carro bomba. El keyword matching sabe encontrar; no sabe decidir.
"""

import os, sys, time, json, logging, argparse
from datetime import date, datetime, timezone

import requests

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
log = logging.getLogger("clasificar_senales")

# ── Config ──────────────────────────────────────────────────────────────────
API            = "https://api.anthropic.com/v1/messages"
MODELO         = os.getenv("RADAR_MODELO", "claude-sonnet-4-5")

MAX_PASAJES     = int(os.getenv("RADAR_MAX_PASAJES", "25"))
VENTANA_FUSION  = int(os.getenv("RADAR_VENTANA_FUSION", "900"))
LIMITE_DOCS     = int(os.getenv("RADAR_LIMITE_DOCS", "50"))

T_DOC = "core_documents"
T_SEN = "core_senales"

def _exigir_api_key() -> str:
    """Falla temprano y con un mensaje legible.

    Antes esto era os.environ["ANTHROPIC_API_KEY"] a nivel de módulo: si el
    secret faltaba o estaba con otro nombre, el script moría con un KeyError
    crudo en el import, sin decir qué faltaba. Es exactamente lo que pasó en
    la corrida del 31-08.
    """
    clave = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not clave:
        log.error("Falta ANTHROPIC_API_KEY. En GitHub: Settings → Secrets and "
                  "variables → Actions → New repository secret, con ese nombre "
                  "exacto. El workflow lo pasa como env al job.")
        sys.exit(2)
    return clave


# ── Pasajes ─────────────────────────────────────────────────────────────────

def fusionar_pasajes(filas: list) -> list:
    """Une pasajes solapados: 12 apariciones de 'bomberos' en la misma página
    son un solo tramo de texto, no 12 pasajes."""
    if not filas:
        return []
    orden  = sorted(filas, key=lambda f: f["pos"])
    grupos = [[orden[0]]]
    for f in orden[1:]:
        if f["pos"] - grupos[-1][-1]["pos"] <= VENTANA_FUSION:
            grupos[-1].append(f)
        else:
            grupos.append([f])

    pasajes = []
    for g in grupos:
        texto    = max((x["pasaje"] for x in g), key=len)
        terminos = sorted({x["termino"] for x in g})
        pasajes.append(f"[keywords: {', '.join(terminos)}]\n{texto}")
    return pasajes[:MAX_PASAJES]


# ── Modelo ──────────────────────────────────────────────────────────────────

def pedir_clasificacion(doc: dict, pasajes: list):
    contexto = (
        f"Organismo: {doc.get('organismo') or doc.get('region')}\n"
        f"Region: {doc.get('region')}\n"
        f"Documento: {doc.get('title')}\n"
        f"Fecha del documento: {doc.get('act_date') or 'no informada'}\n"
        f"Fuente: {doc.get('document_url')}\n\n"
        f"Pasajes ({len(pasajes)}):\n\n" + "\n\n---\n\n".join(pasajes)
    )
    cuerpo = {
        "model": MODELO,
        "max_tokens": 4096,
        "system": [{"type": "text", "text": SYSTEM,
                    "cache_control": {"type": "ephemeral"}}],
        "tools": [herramienta()],
        "tool_choice": {"type": "tool", "name": "registrar_senales"},
        "messages": [{"role": "user", "content": contexto}],
    }
    cabeceras = {"x-api-key": _exigir_api_key(),
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

def construir_fila(senal: dict, doc: dict) -> dict:
    fecha  = senal.get("fecha_evento") or doc.get("act_date") or date.today().isoformat()
    codigo = senal.get("codigo_bip") or senal.get("acuerdo_numero")

    dedupe = sb.rpc("fn_core_dedupe_key", {
        "p_organismo_id": doc.get("organismo_id"), "p_categoria": senal["categoria"],
        "p_etapa": senal["etapa"], "p_codigo": codigo,
        "p_fecha": fecha, "p_monto": senal.get("monto_clp")})
    score = sb.rpc("fn_core_score", {
        "p_etapa": senal["etapa"], "p_unidades": senal.get("unidades"),
        "p_monto_clp": senal.get("monto_clp"),
        "p_prioridad": doc.get("organismo_prioridad") or 3,
        "p_confianza": senal.get("confianza", 0.5)})

    return {
        "document_id": doc["id"], "source_id": doc.get("source_id"),
        "organismo_id": doc.get("organismo_id"),
        "fuente_tipo": doc.get("fuente_tipo") or "acuerdo_core",
        "fuente_url": doc["document_url"],
        "region": doc.get("region"), "comuna": senal.get("comuna") or doc.get("comuna"),
        "categoria": senal["categoria"], "etapa": senal["etapa"],
        "titulo": senal["titulo"][:500], "resumen": senal.get("resumen"),
        "por_que_importa": senal.get("por_que_importa"),
        "unidades": senal.get("unidades"), "monto_clp": senal.get("monto_clp"),
        "monto_raw": senal.get("monto_raw"), "codigo_bip": senal.get("codigo_bip"),
        "acuerdo_numero": senal.get("acuerdo_numero"), "sesion_fecha": doc.get("act_date"),
        "fecha_evento": fecha, "confianza": senal.get("confianza"), "score": score,
        "dedupe_key": dedupe, "clasificador_modelo": MODELO,
        "prompt_version": PROMPT_VERSION,
        "clasificado_at": datetime.now(timezone.utc).isoformat(),
    }


def documentos_pendientes(doc_id):
    params = {
        "select": "id,source_id,organismo_id,region,comuna,title,act_date,document_url,"
                  "extracted_text,core_organismos(nombre,prioridad),core_sources(fuente_tipo)",
        "order": "act_date.desc.nullslast",
        "limit": str(LIMITE_DOCS),
    }
    if doc_id:
        params["id"] = f"eq.{doc_id}"
    else:
        params["prefiltro_ok"]   = "is.true"
        params["clasificado_at"] = "is.null"
        # Las notas de prensa las clasifica ingesta_prensa.py en lote. Si una
        # corrida suya se cae a medio camino, sus documentos quedarían acá y se
        # procesarían de a uno: correcto, pero carísimo para textos de 300
        # caracteres. Este filtro deja cada vía con lo suyo.
        params["extraction_method"] = "neq.rss"

    docs = sb.select(T_DOC, params)
    for d in docs:
        org = d.pop("core_organismos", None) or {}
        src = d.pop("core_sources", None) or {}
        d["organismo"]            = org.get("nombre")
        d["organismo_prioridad"]  = org.get("prioridad")
        d["fuente_tipo"]          = src.get("fuente_tipo")
    return docs


def main() -> int:
    ap = argparse.ArgumentParser(description="Clasificador de señales del radar CORE")
    ap.add_argument("--doc", type=int, help="clasificar solo este documento")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    docs = documentos_pendientes(args.doc)
    log.info("Documentos por clasificar: %d", len(docs))

    total = tok_in = tok_out = 0
    for doc in docs:
        log.info("[doc %s] %s", doc["id"], (doc.get("title") or "")[:70])
        filas = sb.rpc("fn_core_prefiltro",
                       {"p_texto": (doc.get("extracted_text") or "")[:400_000],
                        "p_ventana": 700}) or []
        pasajes = fusionar_pasajes([f for f in filas if not f["es_negativo"]])
        log.info("   %d apariciones → %d pasajes al modelo", len(filas), len(pasajes))
        if not pasajes:
            continue

        senales, uso = pedir_clasificacion(doc, pasajes)
        tok_in  += uso.get("input_tokens", 0)
        tok_out += uso.get("output_tokens", 0)
        log.info("   señales: %d", len(senales))

        filas_senal = [construir_fila(s, doc) for s in senales]
        for f in filas_senal:
            log.info("   → [%3d] %s/%s: %s", f["score"], f["categoria"],
                     f["etapa"], f["titulo"][:80])

        if not args.dry_run:
            if filas_senal:
                sb.insert(T_SEN, filas_senal, on_conflict="dedupe_key", ignorar_dup=True)
            sb.update(T_DOC, {"id": f"eq.{doc['id']}"}, {
                "status": "clasificado",
                "clasificado_at": datetime.now(timezone.utc).isoformat(),
                "clasificador_modelo": MODELO, "prompt_version": PROMPT_VERSION,
                "tokens_in": uso.get("input_tokens"), "tokens_out": uso.get("output_tokens")})
        total += len(filas_senal)

    log.info("Señales nuevas: %d | tokens in/out: %s/%s", total, f"{tok_in:,}", f"{tok_out:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
