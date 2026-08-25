"""
sync_adjuntos.py
================
LISTA (no descarga) las bases y anexos de licitaciones de Mercado Público y
registra la metadata en mp_licitacion_adjuntos. El archivo en sí se trae
recién cuando alguien lo pide desde el panel — eso lo hace la Edge Function
`mp-obtener-adjunto`, no este script.

Va en mp_sync/ junto a los demás scripts del repo super-succotash.

Por qué así: listar cuesta 2 requests por licitación (la ficha y la grilla de
anexos) — barato, se puede hacer para todo lo priorizado cada 2h. Descargar
cuesta el peso de los archivos (bases de 5-40 MB por licitación), y el 95%
nunca se va a abrir. Se lista todo, se trae lo que se usa.

Flujo verificado contra producción (25-08-2026):
    1. DetailsAcquisition.aspx?idlicitacion=CODIGO
         → link cifrado a VerAntecedentes.aspx?enc=...
    2. VerAntecedentes.aspx?enc=...
         → grilla con nombre, tipo, descripción y fecha de cada anexo
    (la descarga real — postback con VIEWSTATE — vive en la Edge Function)

Alcance del listado automático (vista v_mp_adjuntos_pendientes):
  - vigentes que pegan con el Core de algún usuario, o
  - marcadas 'interesa'/'quizas', o
  - lo pedido a mano en mp_adjuntos_cola (el panel), que va primero.

Programar: cada 2 h, min :35 (detrás de licitaciones :15 y CA :05).

Env:
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  ADJ_MAX_LIC_POR_RUN   default 80    listar es barato; el doble que antes
  SLEEP_BETWEEN         default 2.0
"""

import os, re, time, html, logging
import requests

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("sync_adjuntos")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"

MAX_LIC = int(os.getenv("ADJ_MAX_LIC_POR_RUN", "80"))
SLEEP   = float(os.getenv("SLEEP_BETWEEN", "2.0"))

MP_BASE = "https://www.mercadopublico.cl/Procurement/Modules"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

T_ADJ, T_COLA = "mp_licitacion_adjuntos", "mp_adjuntos_cola"

_sb = requests.Session()
_sb.headers.update({"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"})
_mp = requests.Session()
_mp.headers.update({"User-Agent": UA})


def sb_select(table, params):
    r = _sb.get(f"{SB_REST}/{table}", params=params, timeout=60)
    r.raise_for_status(); return r.json()

def sb_upsert(table, rows, on_conflict):
    if not rows: return
    r = _sb.post(f"{SB_REST}/{table}", params={"on_conflict": on_conflict},
        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        json=rows, timeout=60)
    r.raise_for_status()


def codigos_pendientes(limite):
    cola = sb_select(T_COLA, {"select": "codigo", "estado": "eq.pendiente",
                              "order": "created_at.asc", "limit": str(limite)})
    codigos = [c["codigo"] for c in cola]
    if len(codigos) < limite:
        resto = sb_select("v_mp_adjuntos_pendientes",
                          {"select": "codigo", "limit": str(limite - len(codigos))})
        vistos = set(codigos)
        codigos += [r["codigo"] for r in resto if r["codigo"] not in vistos]
    return codigos[:limite]


def link_antecedentes(codigo):
    r = _mp.get(f"{MP_BASE}/RFB/DetailsAcquisition.aspx",
                params={"idlicitacion": codigo}, timeout=60)
    r.raise_for_status()
    m = re.search(r'Attachment/VerAntecedentes\.aspx\?enc=[^"\']+', r.text)
    return f"{MP_BASE}/{html.unescape(m.group(0))}" if m else None


def parsear_grilla(page):
    """Filas de la grilla: cada botón grdAttachment$ctlNN$grdIbtnView es un anexo."""
    filas = []
    for m in re.finditer(r'name="(grdAttachment\$ctl(\d+)\$grdIbtnView)"', page):
        target, nn = m.group(1), m.group(2)
        celdas = dict(re.findall(
            r'id="grdAttachment_ctl%s_lbl(\w+)"[^>]*>([^<]*)<' % nn, page))
        filas.append({
            "target": target,
            "nombre": html.unescape(celdas.get("Name", celdas.get("Nombre", ""))).strip(),
            "tipo": html.unescape(celdas.get("Type", celdas.get("Tipo", ""))).strip(),
            "descripcion": html.unescape(celdas.get("Description", celdas.get("Descripcion", ""))).strip(),
            "fecha": html.unescape(celdas.get("Date", celdas.get("Fecha", ""))).strip(),
        })
    return filas


def procesar(codigo):
    """Sólo metadata. storage_path queda NULL — la Edge Function lo llena al primer clic."""
    url = link_antecedentes(codigo)
    if not url:
        sb_upsert(T_COLA, [{"codigo": codigo, "estado": "sin_anexos"}], "codigo")
        return 0
    page = _mp.get(url, timeout=60).text
    filas = parsear_grilla(page)
    if not filas:
        sb_upsert(T_COLA, [{"codigo": codigo, "estado": "sin_anexos"}], "codigo")
        return 0
    registros = [{
        "codigo_externo": codigo,
        "nombre_archivo": f["nombre"] or f["target"],   # el nombre real llega al descargar
        "tipo": f["tipo"] or None,
        "descripcion": f["descripcion"] or None,
        "fecha_publicado": f["fecha"] or None,
        "postback_target": f["target"],                 # lo que necesita la Edge Function
        "es_bases": bool(re.search(r"\bbases?\b", f["tipo"] + " " + f["nombre"], re.I)),
    } for f in filas]
    sb_upsert(T_ADJ, registros, "codigo_externo,nombre_archivo")
    sb_upsert(T_COLA, [{"codigo": codigo, "estado": "listado"}], "codigo")
    return len(registros)


def main():
    codigos = codigos_pendientes(MAX_LIC)
    log.info("Listando anexos de %d licitaciones", len(codigos))
    total = 0
    for i, c in enumerate(codigos, 1):
        try:
            n = procesar(c); total += n
            log.info("[%d/%d] %s → %d anexos listados", i, len(codigos), c, n)
        except Exception as e:
            log.error("[%d/%d] %s FALLÓ: %r", i, len(codigos), c, e)
            sb_upsert(T_COLA, [{"codigo": c, "estado": "error"}], "codigo")
        time.sleep(SLEEP)
    try:
        sb_upsert("data_freshness", [{
            "dataset": "mp_adjuntos",
            "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "rows_changed": total, "source": "sync_adjuntos.py (listado)"}], "dataset")
    except Exception as e:
        log.warning("No pude estampar data_freshness: %r", e)
    log.info("Listo: %d anexos listados.", total)


if __name__ == "__main__":
    main()
