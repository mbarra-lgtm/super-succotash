"""
sync_crm.py
===========
Actualiza el estado de licitaciones vinculadas al CRM Odoo.
Programar: cada 30 minutos (o 1 vez al día si prefieres).

Lee mp_tender_code de crm_projects y sincroniza el estado actual desde MP.

Escribe en DOS lados:
  · crm_mp_licitaciones / crm_mp_items / crm_mp_adjudicaciones
    → vista enriquecida con el vínculo al lead de Odoo.
  · mp_licitaciones / mp_licitacion_fechas
    → espejo canónico que lee v_daily_comercial (y el resto de los paneles).

Sin el segundo upsert, una licitación que el barrido de sync_activas.py todavía
no alcanzó queda invisible para el Daily Comercial aunque este script sí la haya
traído: el dato existe, pero en una tabla que la vista no mira.
"""

import os, time, json, hashlib, logging, requests, random
from datetime import datetime, timezone
from typing import Optional

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

# Después de load_dotenv: sb_client lee las credenciales al importarse.
import sb_client as sb

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("sync_crm")

MP_TICKET    = os.environ["TICKET_CRM"]
MP_API       = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"
SLEEP        = float(os.getenv("SLEEP_BETWEEN", "2.0"))
CRM_LIMIT    = int(os.getenv("CRM_LIMIT", "2000"))

ESTADOS_FINALES = {"adjudicada","adjudicado","desierta","revocada","revocado"}

T_PROJ  = "crm_projects"
T_LIC   = "crm_mp_licitaciones"
T_ITEMS = "crm_mp_items"
T_ADJ   = "crm_mp_adjudicaciones"

# Espejo canónico (mismas tablas que escribe sync_activas.py)
T_MP_LIC    = "mp_licitaciones"
T_MP_FECHAS = "mp_licitacion_fechas"

# PostgREST corta las respuestas en 1000 filas por defecto, así que los lotes se
# piden paginados: con `limit` a secas se perdían las oportunidades del final.
PAGE = 1000

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

def _mp_get(params):
    r = _session.get(MP_API, params={**params, "ticket": MP_TICKET}, timeout=45)
    if r.status_code == 429:
        log.warning("429 — esperando 60s...")
        time.sleep(60)
        r = _session.get(MP_API, params={**params, "ticket": MP_TICKET}, timeout=45)
    r.raise_for_status()
    return r.json()

def _sb_headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}

def _sb_get(table, filters={}, select="*", limit=5000):
    """Antes devolvía [] ante error, lo que se leía como 'no hay datos'."""
    params = {k: f"eq.{v}" for k, v in filters.items()}
    params.update({"select": select, "limit": str(limit)})
    return sb.select(table, params)

def _sb_get_paginado(table, select, params=None, page=PAGE, tope=20000):
    """GET a PostgREST paginado por offset. Devuelve todas las filas.

    El `break` ante error devolvía las páginas leídas hasta ahí como si fueran el
    conjunto completo: el llamador veía menos oportunidades de CRM de las que hay
    y las que faltaban simplemente no se sincronizaban. Ahora un error levanta.
    """
    filas, offset = [], 0
    while offset < tope:
        q = dict(params or {})
        q.update({"select": select, "limit": str(page), "offset": str(offset)})
        lote = sb.select(table, q) or []
        filas.extend(lote)
        if len(lote) < page:
            break
        offset += page
    return filas


def _leer_candidatos_crm():
    """
    Oportunidades vivas del CRM que tienen código de licitación.

    Antes esto era un `select ... limit=CRM_LIMIT` sin filtro ni orden sobre
    crm_projects (3.400+ filas), y PostgREST devolvía como máximo 1.000: las
    oportunidades que caían más allá de esa ventana nunca se consultaban a
    Mercado Público. Filtrando por código + activas quedan ~200 filas, entra
    todo con margen y además no se gasta cuota de API en leads muertos.
    """
    filas = _sb_get_paginado(
        T_PROJ,
        select="odoo_id,name,mp_tender_code",
        params={
            "mp_tender_code": "not.is.null",
            "is_active": "is.true",
            "won_status": "in.(pending,won)",
            "order": "odoo_id.desc",
        },
        tope=CRM_LIMIT,
    )
    seen, candidatos = set(), []
    for p in filas:
        # El CRM guarda "1285325-1-LR26/3" (código + ítem); a Mercado Público se
        # le pregunta solo por el código, igual que hace v_daily_comercial.
        code = str(p.get("mp_tender_code") or "").strip().split("/")[0].strip()
        if not code or code in seen:
            continue
        seen.add(code)
        candidatos.append({"codigo": code, "odoo_id": p.get("odoo_id"), "name": p.get("name")})
    return candidatos


_sb_upsert = sb.upsert

def _sb_delete(table, col, val):
    sb.delete(table, {col: f"eq.{val}"})

def _limpiar_sobrantes(codigo, item_rows, adj_rows):
    """Borra items y adjudicaciones que ya no vienen en el detalle de MP.

    Se llama DESPUÉS de escribir, no antes: así, si el upsert falla, la
    licitación conserva lo que tenía en vez de quedar vacía. Las bajas reales
    (un item que MP eliminó) se siguen propagando, que es para lo que existía el
    borrado original.
    """
    vivos = [str(r["item_no"]) for r in item_rows]
    if vivos:
        sb.delete(T_ITEMS, {"codigo_externo": f"eq.{codigo}",
                            "item_no": f"not.in.({','.join(vivos)})"})
    else:
        sb.delete(T_ITEMS, {"codigo_externo": f"eq.{codigo}"})

    previas = sb.select(T_ADJ, {"select": "item_no,proveedor_rut",
                                "codigo_externo": f"eq.{codigo}"})
    vivas = {(str(r.get("item_no")), str(r.get("proveedor_rut"))) for r in adj_rows}
    for p in previas:
        if (str(p.get("item_no")), str(p.get("proveedor_rut"))) not in vivas:
            sb.delete(T_ADJ, {"codigo_externo": f"eq.{codigo}",
                              "item_no": f"eq.{p['item_no']}",
                              "proveedor_rut": f"eq.{p['proveedor_rut']}"})

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

def _num(v):
    try: return float(str(v).replace(",", ".").strip())
    except: return None

def _should_skip(codigo):
    rows = _sb_get(T_LIC, {"codigo_externo": codigo}, "estado,raw_hash", 1)
    if not rows: return False
    row = rows[0]
    if str(row.get("estado") or "").strip().lower() not in ESTADOS_FINALES: return False
    if not row.get("raw_hash"): return False
    items = _sb_get(T_ITEMS, {"codigo_externo": codigo}, "item_no", 1)
    if not items: return False
    # Aunque la licitación esté en estado final, si todavía no llegó al espejo
    # canónico hay que traerla: es la tabla que lee v_daily_comercial.
    if not _sb_get(T_MP_LIC, {"codigo_externo": codigo}, "codigo_externo", 1):
        return False
    return True

def _upsert_espejo_canonico(codigo, lic, raw_h):
    """
    Réplica del cabezal que escribe sync_activas.py en mp_licitaciones, con el
    payload completo en `raw`: de ahí salen FechaFinal (fin de preguntas) y
    FechaPubRespuestas, que v_daily_comercial lee del JSON y que la tabla
    crm_mp_licitaciones no guarda.
    """
    fechas = lic.get("Fechas") or {}
    cab = {
        "codigo_externo":     codigo,
        "nombre":             lic.get("Nombre"),
        "descripcion":        lic.get("Descripcion"),
        "tipo":               lic.get("Tipo"),
        "estado":             lic.get("Estado"),
        "codigo_estado":      lic.get("CodigoEstado"),
        "moneda":             lic.get("Moneda"),
        "raw":                lic,   # monto_estimado es columna generada desde raw
        "fecha_publicacion":  _ts(fechas.get("FechaPublicacion")),
        "fecha_cierre":       _ts(fechas.get("FechaCierre")),
        "fecha_adjudicacion": _ts(fechas.get("FechaAdjudicacion")),
        # Sin raw_hash: es el testigo que usa sync_activas.py para saber si ya
        # escribio cabecera + items + comprador. Al estamparlo aca (mismo _hash
        # sobre el mismo payload) sync_activas veia "sin cambio" y nunca escribia
        # los hijos de las licitaciones del CRM, justo las que mas importan.
        "last_sync_at":       datetime.now(timezone.utc).isoformat(),
    }
    fechas_row = {
        "codigo_externo":         codigo,
        "fecha_publicacion":      _ts(fechas.get("FechaPublicacion")),
        "fecha_cierre":           _ts(fechas.get("FechaCierre")),
        "fecha_adjudicacion":     _ts(fechas.get("FechaAdjudicacion")),
        "fecha_apertura_tecnica": _ts(fechas.get("FechaActoAperturaTecnica")),
    }
    _sb_upsert(T_MP_LIC,    "codigo_externo", [cab])
    _sb_upsert(T_MP_FECHAS, "codigo_externo", [fechas_row])


def main():
    log.info("=== sync_crm ===")

    # Leer candidatos desde CRM (paginado y filtrado)
    candidatos = _leer_candidatos_crm()
    log.info("%d licitaciones CRM a verificar", len(candidatos))
    ok = skip = err = espejo = 0

    for cand in candidatos:
        codigo = cand["codigo"]
        if _should_skip(codigo):
            skip += 1
            continue

        try:
            data  = _mp_get({"codigo": codigo})
            time.sleep(SLEEP)
            lics  = data.get("Listado") or []
            if not lics: continue
            lic   = lics[0]

            fechas = lic.get("Fechas") or {}
            comp   = lic.get("Comprador") or {}
            items  = ((lic.get("Items") or {}).get("Listado")) or []
            raw_h  = _hash(lic)

            # Verificar si cambió
            existing = _sb_get(T_LIC, {"codigo_externo": codigo}, "raw_hash", 1)
            if existing and existing[0].get("raw_hash") == raw_h:
                # Nada cambió en MP, pero puede faltar en el espejo canónico
                # (por ejemplo si se agregó después de la primera ingesta).
                if not _sb_get(T_MP_LIC, {"codigo_externo": codigo}, "codigo_externo", 1):
                    _upsert_espejo_canonico(codigo, lic, raw_h)
                    espejo += 1
                ok += 1
                continue

            cab = {
                "codigo_externo":             codigo,
                "nombre":                     lic.get("Nombre"),
                "tipo":                       lic.get("Tipo"),
                "estado":                     lic.get("Estado"),
                "moneda":                     lic.get("Moneda"),
                "fecha_publicacion":          _ts(fechas.get("FechaPublicacion")),
                "fecha_cierre":              _ts(fechas.get("FechaCierre")),
                "fecha_adjudicacion":         _ts(fechas.get("FechaAdjudicacion")),
                "organismo_nombre":           comp.get("NombreOrganismo"),
                "organismo_region":           comp.get("RegionUnidad"),
                "crm_lead_odoo_id":           cand.get("odoo_id"),
                "crm_lead_name":              cand.get("name"),
                "raw_hash":                   raw_h,
                "last_sync_at":               datetime.now(timezone.utc).isoformat(),
            }
            _sb_upsert(T_LIC, "codigo_externo", [cab])

            # Espejo canónico: es el que lee v_daily_comercial.
            _upsert_espejo_canonico(codigo, lic, raw_h)
            espejo += 1

            # Items y adjudicaciones. El borrado va DESPUÉS del upsert (ver
            # _limpiar_sobrantes): borrar primero y fallar al escribir dejaba la
            # licitación sin items ni adjudicaciones, y nada las reponía.
            item_rows, adj_rows = [], []
            seen_i, seen_a = set(), set()
            for it in items:
                if not isinstance(it, dict): continue
                try: correl = int(it.get("Correlativo"))
                except: continue
                if correl in seen_i: continue
                seen_i.add(correl)
                item_rows.append({
                    "codigo_externo":  codigo, "item_no": correl,
                    "nombre_producto": str(it.get("NombreProducto") or "").strip() or None,
                    "cantidad":        _num(it.get("Cantidad")),
                })
                adj = it.get("Adjudicacion")
                for a in (adj if isinstance(adj, list) else ([adj] if isinstance(adj, dict) else [])):
                    if not isinstance(a, dict): continue
                    rut = str(a.get("RutProveedor") or "").strip() or None
                    if not rut: continue
                    key = f"{codigo}|{correl}|{rut}"
                    if key in seen_a: continue
                    seen_a.add(key)
                    adj_rows.append({
                        "codigo_externo":  codigo, "item_no": correl,
                        "proveedor_rut":   rut,
                        "proveedor_nombre":str(a.get("NombreProveedor") or "").strip() or None,
                        "monto_total":     _num(a.get("MontoTotal")),
                        "fecha_resolucion":_ts(a.get("FechaResolucion")),
                    })

            if item_rows: _sb_upsert(T_ITEMS, "codigo_externo,item_no", item_rows)
            if adj_rows:  _sb_upsert(T_ADJ, "codigo_externo,item_no,proveedor_rut", adj_rows)
            _limpiar_sobrantes(codigo, item_rows, adj_rows)
            log.info("Actualizado: %s | estado=%s", codigo, lic.get("Estado"))
            ok += 1

        except Exception as e:
            log.warning("Error %s: %s", codigo, repr(e))
            err += 1

    log.info("Resultado: ok=%d skip=%d err=%d | espejo mp_licitaciones=%d",
             ok, skip, err, espejo)

    # Rojo si se perdió alguna escritura hacia Supabase.
    sb.exit_si_hubo_fallos()

if __name__ == "__main__":
    main()
