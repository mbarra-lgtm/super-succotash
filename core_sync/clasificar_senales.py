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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("clasificar_senales")

# ── Config ──────────────────────────────────────────────────────────────────
API            = "https://api.anthropic.com/v1/messages"
MODELO         = os.getenv("RADAR_MODELO", "claude-sonnet-4-5")
PROMPT_VERSION = "radar-v1"
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]

MAX_PASAJES     = int(os.getenv("RADAR_MAX_PASAJES", "25"))
VENTANA_FUSION  = int(os.getenv("RADAR_VENTANA_FUSION", "900"))
LIMITE_DOCS     = int(os.getenv("RADAR_LIMITE_DOCS", "50"))

T_DOC = "core_documents"
T_SEN = "core_senales"

SYSTEM = """Eres analista de inteligencia comercial de Bertonati Vehiculos Especiales,
fabricante chileno de ambulancias, carros bomba, vehiculos blindados y carrozados
especiales.

Tu trabajo es leer pasajes de actas y acuerdos de Consejos Regionales (CORE) y
concejos municipales de Chile, y decidir cuales representan una oportunidad
comercial real para Bertonati.

QUE ES UNA SEÑAL (registrala):
- Aprobacion de financiamiento (FNDR, circular 33, subvencion municipal, convenio)
  para ADQUIRIR ambulancias, carros bomba, material mayor de bomberos, vehiculos
  blindados o moviles de emergencia.
- Licitacion, adjudicacion o entrega de esos mismos vehiculos.
- Acuerdos que comprometen presupuesto futuro para renovacion de flota de
  emergencia, aunque no den la cifra exacta.

QUE NO ES UNA SEÑAL (no la registres, por mucho que aparezca la palabra):
- Obras y edificios: construccion o reparacion de cuarteles de bomberos, postas,
  hospitales. Bertonati vende vehiculos, no inmuebles.
- Lineas de presupuesto genericas: glosas tipo "29 ADQUISICION DE ACTIVOS NO
  FINANCIEROS / 03 Vehiculos", "dotacion maxima de vehiculos", programas de
  funcionamiento del propio Gobierno Regional.
- Vehiculos que no son del rubro: camionetas municipales, buses, maquinaria
  agricola, camiones aljibe, retroexcavadoras.
- Menciones de contexto: felicitaciones a bomberos, fiscalizaciones, homenajes,
  cuentas publicas, agendas de actividades.
- Compras ya ejecutadas hace mas de 18 meses.

REGLAS DE SALIDA:
- Un hecho = una señal. Si el mismo proyecto aparece en cinco pasajes del mismo
  documento, entregalo UNA sola vez.
- No inventes cifras ni unidades. Si el pasaje no dice cuantas unidades o cuanto
  dinero, deja el campo en null. Un null honesto vale mas que un numero inventado.
- Los montos en actas chilenas suelen venir en miles de pesos ("M$354.294" =
  354.294.000 pesos). Convierte a pesos en monto_clp y deja el texto original
  en monto_raw.
- confianza refleja cuan seguro estas de que ESTO es una compra de vehiculos del
  rubro: 0.9+ solo si el pasaje lo dice explicitamente.
- Ante la duda, no registres. Un correo con tres señales reales se lee todos los
  dias; uno con cuarenta se ignora en una semana."""

HERRAMIENTA = {
    "name": "registrar_senales",
    "description": "Registra las señales comerciales encontradas. Lista vacía si no hay ninguna.",
    "input_schema": {
        "type": "object",
        "properties": {
            "senales": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "categoria": {"type": "string", "enum": [
                            "ambulancia", "carro_bomba", "blindado",
                            "movil_policial", "rescate", "carroceria", "otro"]},
                        "etapa": {"type": "string", "enum": [
                            "idea", "financiamiento_aprobado", "licitacion_publicada",
                            "adjudicada", "entregada", "rechazada"]},
                        "titulo":          {"type": "string", "description": "Una línea: quién aprueba qué"},
                        "resumen":         {"type": "string", "description": "2-3 frases"},
                        "por_que_importa": {"type": "string", "description": "Lectura comercial y timing"},
                        "unidades":        {"type": ["integer", "null"]},
                        "monto_clp":       {"type": ["number", "null"], "description": "En pesos, ya convertido"},
                        "monto_raw":       {"type": ["string", "null"], "description": "El monto tal cual aparece"},
                        "acuerdo_numero":  {"type": ["string", "null"]},
                        "codigo_bip":      {"type": ["string", "null"]},
                        "comuna":          {"type": ["string", "null"]},
                        "fecha_evento":    {"type": ["string", "null"], "description": "YYYY-MM-DD si el pasaje la da"},
                        "confianza":       {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["categoria", "etapa", "titulo", "resumen",
                                 "por_que_importa", "confianza"],
                },
            }
        },
        "required": ["senales"],
    },
}


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
        "tools": [HERRAMIENTA],
        "tool_choice": {"type": "tool", "name": "registrar_senales"},
        "messages": [{"role": "user", "content": contexto}],
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
