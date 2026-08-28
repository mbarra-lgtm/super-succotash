"""
sync_activas.py
===============
Busca licitaciones ACTIVAS nuevas o modificadas.
Programar: cada 1 hora (ver mp-licitaciones-oc.yml).

Lógica:
  - Trae el listado completo de estado=activas
  - Prefetch de hashes en bulk (troceado) de TODO el listado
  - Procesa PRIMERO las que no están en BD: una licitación nueva entra el mismo
    día sin importar dónde caiga en el orden alfabético
  - Con el cupo restante refresca en round-robin, con un cursor que NO se
    resetea por fecha (ver nota abajo)
  - Solo escribe las que son nuevas o cambiaron

Nota sobre el cursor (bug corregido 2026-08-28)
-----------------------------------------------
El cursor se reseteaba a 0 cada medianoche UTC. Con ~4.000 activas, ventana de
200 y corridas cada 2 h (12/día = 2.400 posiciones), el barrido volvía todos los
días al mismo tramo inicial y la cola de la lista ordenada nunca se visitaba:
las activas sobre la posición ~2.400 acumulaban 5+ días sin sincronizar y las
licitaciones nuevas que caían ahí no se insertaban nunca. El cursor ahora es
round-robin real: avanza, da la vuelta con módulo y persiste entre días.
"""

import os, sys, time, json, random, logging, requests
from datetime import datetime, timezone
from typing import Optional

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

from cursor_store import load_cursor, save_cursor

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("sync_activas")

# ── Config ──────────────────────────────────
MP_TICKET    = os.environ["TICKET_ACTIVAS"]
MP_API       = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"

SLEEP        = float(os.getenv("SLEEP_BETWEEN", "2.0"))
MAX_POR_RUN  = int(os.getenv("LIC_MAX_POR_RUN", "100"))   # licitaciones por ejecución
# Cupo maximo de la ventana que se reserva a licitaciones que aun no estan en BD.
# Por defecto toda la ventana: una corrida puede ser 100% altas si hay atraso.
MAX_NUEVAS   = int(os.getenv("LIC_MAX_NUEVAS", str(MAX_POR_RUN)))
# Codigos por request al prefetch de hashes. El filtro va en la URL (in.(...)),
# asi que trocear evita pasarse del largo maximo con listados de miles.
HASH_CHUNK   = int(os.getenv("LIC_HASH_CHUNK", "400"))
CURSOR_FILE  = os.getenv("LIC_CURSOR_FILE",
               os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cursor_activas.json"))

T_LIC       = "mp_licitaciones"
T_COMP      = "mp_licitacion_comprador"
T_FECHAS    = "mp_licitacion_fechas"
T_ITEMS     = "mp_licitacion_items"
T_ADJ       = "mp_adjudicaciones"

# ── HTTP ─────────────────────────────────────
_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

class RateLimited(Exception):
    """429 persistente: la cuota del ticket esta agotada, no es un error del codigo."""


def _mp_get(params: dict, intentos: int = 4) -> dict:
    """GET a la API de MP con backoff ante 429/5xx.

    Antes se reintentaba UNA sola vez ante 429 y, si volvia a fallar, la
    excepcion la absorbia el except del bucle: la licitacion se contaba como
    error, el cursor igual avanzaba y esa licitacion quedaba saltada. Ahora el
    429 persistente sube como RateLimited para que main() corte la corrida sin
    mover el cursor por encima de lo efectivamente procesado.
    """
    espera = 30
    for intento in range(1, intentos + 1):
        r = _session.get(MP_API, params={**params, "ticket": MP_TICKET}, timeout=45)
        if r.status_code == 429 or r.status_code >= 500:
            if intento == intentos:
                if r.status_code == 429:
                    raise RateLimited(f"429 tras {intentos} intentos")
                r.raise_for_status()
            log.warning("HTTP %s — reintento %d/%d en %ds",
                        r.status_code, intento, intentos, espera)
            time.sleep(espera)
            espera = min(espera * 2, 240)
            continue
        r.raise_for_status()
        return r.json()
    raise RateLimited("sin respuesta utilizable")

def _sb_headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}

def _sb_upsert(table, on_conflict, rows):
    if not rows: return
    url = f"{SB_REST}/{table}"
    r = requests.post(url, headers=_sb_headers(),
                      params={"on_conflict": on_conflict} if on_conflict else {},
                      json=rows, timeout=60)
    if not r.ok:
        log.error("Supabase %s error: %s", table, r.text[:200])

def _sb_delete(table, col, val):
    requests.delete(f"{SB_REST}/{table}", headers=_sb_headers(),
                    params={col: f"eq.{val}"}, timeout=30)

def _sb_hashes_bulk(codigos: list) -> dict:
    """Retorna dict codigo_externo -> raw_hash de las que ya existen en BD.

    Trocea en lotes de HASH_CHUNK: el filtro viaja en la URL y con listados de
    miles de codigos un solo request se pasa del largo maximo y vuelve vacio,
    lo que haria ver TODAS las licitaciones como nuevas.
    """
    if not codigos: return {}
    out = {}
    for i in range(0, len(codigos), HASH_CHUNK):
        lote = codigos[i:i + HASH_CHUNK]
        r = requests.get(f"{SB_REST}/{T_LIC}", headers=_sb_headers(),
                         params={"select": "codigo_externo,raw_hash",
                                 "codigo_externo": f"in.({','.join(lote)})",
                                 "limit": str(len(lote)+1)}, timeout=30)
        if not r.ok:
            # Falla parcial: se aborta el prefetch entero. Devolver un dict
            # incompleto marcaria como "nuevas" licitaciones que si estan.
            raise RuntimeError(f"prefetch de hashes fallo ({r.status_code}): {r.text[:150]}")
        for row in r.json():
            if row.get("codigo_externo"):
                out[row["codigo_externo"]] = row.get("raw_hash")
    return out

# ── Cursor (persistido en Supabase: mp_sync_cursor, key="activas") ───
def _load_cursor() -> int:
    """Posicion del round-robin. NO se resetea por fecha: el barrido de la lista
    de activas dura mas de un dia y reiniciarlo cada medianoche dejaba la cola
    de la lista permanentemente sin visitar."""
    try:
        return max(0, int(load_cursor("activas", {}).get("pos", 0)))
    except Exception:
        return 0

def _save_cursor(pos: int, total: int = 0, nuevas_pend: int = 0):
    # 'date' se conserva solo como marca de la ultima corrida (observabilidad);
    # ya no participa en la decision de resetear.
    save_cursor("activas", {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "pos": pos,
        "total_activas": total,
        "nuevas_pendientes": nuevas_pend,
    })

# ── Parser ───────────────────────────────────
import hashlib

def _hash(obj) -> str:
    return hashlib.md5(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()

def _ts(v) -> Optional[str]:
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

def _parse(lic: dict):
    from dateutil import parser as dtp
    codigo = str(lic.get("CodigoExterno") or "").strip()
    fechas = lic.get("Fechas") or {}
    comp   = lic.get("Comprador") or {}
    items  = ((lic.get("Items") or {}).get("Listado")) or []

    cab = {
        "codigo_externo":    codigo,
        "nombre":            lic.get("Nombre"),
        "descripcion":       lic.get("Descripcion"),
        "tipo":              lic.get("Tipo"),
        "estado":            lic.get("Estado"),
        "codigo_estado":     lic.get("CodigoEstado"),
        "moneda":            lic.get("Moneda"),
        "raw":               lic,  # monto_estimado es columna GENERADA desde raw->>'MontoEstimado'
        "fecha_publicacion": _ts(fechas.get("FechaPublicacion")),
        "fecha_cierre":      _ts(fechas.get("FechaCierre")),
        "fecha_adjudicacion":_ts(fechas.get("FechaAdjudicacion")),
        "raw_hash":          _hash(lic),
        "last_sync_at":      datetime.now(timezone.utc).isoformat(),
    }
    comp_row = {
        "codigo_externo":   codigo,
        "nombre_organismo": str(comp.get("NombreOrganismo") or "").strip() or None,
        "rut_unidad":       str(comp.get("RutUnidad") or "").strip() or None,
        "nombre_unidad":    str(comp.get("NombreUnidad") or "").strip() or None,
        "region_unidad":    str(comp.get("RegionUnidad") or "").strip() or None,
        "comuna_unidad":    str(comp.get("ComunaUnidad") or "").strip() or None,
    }
    fechas_row = {
        "codigo_externo":      codigo,
        "fecha_publicacion":   _ts(fechas.get("FechaPublicacion")),
        "fecha_cierre":        _ts(fechas.get("FechaCierre")),
        "fecha_adjudicacion":  _ts(fechas.get("FechaAdjudicacion")),
        "fecha_apertura_tecnica": _ts(fechas.get("FechaActoAperturaTecnica")),
    }
    item_rows, adj_rows = [], []
    seen_i, seen_a = set(), set()
    for it in items:
        if not isinstance(it, dict): continue
        try: correl = int(it.get("Correlativo"))
        except: continue
        if correl in seen_i: continue
        seen_i.add(correl)
        item_rows.append({
            "codigo_externo":  codigo, "correlativo": correl,
            "nombre_producto": str(it.get("NombreProducto") or "").strip() or None,
            "categoria":       str(it.get("Categoria") or "").strip() or None,
            "cantidad":        _num(it.get("Cantidad")),
            "unidad_medida":   str(it.get("UnidadMedida") or "").strip() or None,
        })
        adj = it.get("Adjudicacion")
        for a in (adj if isinstance(adj, list) else ([adj] if isinstance(adj, dict) else [])):
            if not isinstance(a, dict): continue
            rut = str(a.get("RutProveedor") or "").strip() or None
            if not rut: continue
            key = f"{codigo}|{correl}|{rut}"
            if key in seen_a: continue
            seen_a.add(key)
            mu  = _num(a.get("MontoUnitario"))
            mt  = _num(a.get("MontoTotal"))
            # Cantidad adjudicada a ESTE proveedor; el item de la licitacion es
            # solo respaldo (difieren en ~1,4% de los casos y ahi sobreestima).
            cant = _num(a.get("Cantidad"))
            if cant is None:
                cant = _num(it.get("Cantidad"))
            if mt is None and mu is not None and cant is not None:
                mt = mu * cant
            adj_rows.append({
                "licitacion_id":    codigo, "item_no": correl,
                "proveedor_rut":    rut,
                "proveedor_nombre": str(a.get("NombreProveedor") or "").strip() or None,
                "cantidad":         cant,
                "monto_unitario":   mu,
                "monto_total":      mt,
                "monto_total_fuente": "api_mu_x_cantidad" if mt is not None else None,
                "moneda":           str(a.get("Moneda") or lic.get("Moneda") or "").strip() or None,
                "fecha_resolucion": _ts(a.get("FechaResolucion")),
            })
    return cab, comp_row, fechas_row, item_rows, adj_rows

# ── Main ─────────────────────────────────────
def main():
    log.info("=== sync_activas ===")

    # 1. Listado de activas
    data    = _mp_get({"estado": "activas"})
    listado = data.get("Listado") or []
    # Orden estable: list(set(...)) depende de la aleatorizacion de hash de CPython,
    # que cambia en cada proceso. El cursor quedaba apuntando a una permutacion que
    # ya no existe en la corrida siguiente, asi que la ventana no era "las que
    # faltan" sino una muestra al azar y habia licitaciones nunca visitadas.
    vistos, todos = set(), []
    for l in listado:
        c = str(l.get("CodigoExterno") or "").strip()
        if c and c not in vistos:
            vistos.add(c); todos.append(c)
    todos.sort()
    total = len(todos)
    if not total:
        log.warning("Listado de activas vacío — se aborta sin tocar el cursor")
        return

    # 2. Prefetch raw_hash de TODO el listado (troceado). Sirve para dos cosas:
    #    saber cuáles faltan en BD y detectar cambios sin reescribir de más.
    hashes      = _sb_hashes_bulk(todos)
    nuevas_pend = [c for c in todos if c not in hashes]

    # 3. Ventana de trabajo: primero las altas, después el refresco round-robin.
    #    Las nuevas van con prioridad porque su posición alfabética es azarosa:
    #    esperar a que el cursor pase por ahí es lo que hacía que una licitación
    #    publicada hoy tardara días — o no entrara nunca — en aparecer.
    tramo_nuevas = nuevas_pend[:MAX_NUEVAS]
    set_nuevas   = set(tramo_nuevas)

    cursor = _load_cursor() % total
    cupo   = max(0, MAX_POR_RUN - len(tramo_nuevas))
    tramo_refresh, pos, avance = [], cursor, 0
    while len(tramo_refresh) < cupo and avance < total:
        codigo = todos[pos]
        if codigo not in set_nuevas:
            tramo_refresh.append(codigo)
        pos = (pos + 1) % total
        avance += 1
    nuevo_cursor = pos

    tramo = tramo_nuevas + tramo_refresh
    log.info("Activas: %d total | %d sin ingestar (%d en esta corrida) | "
             "refresco %d desde pos %d", total, len(nuevas_pend),
             len(tramo_nuevas), len(tramo_refresh), cursor)

    # 4. Procesar el tramo: inserta nuevas y re-lee abiertas modificadas
    ok = nuevas = actualizadas = sin_cambio = err = 0
    procesadas_refresh = 0
    corte_cuota = False
    for codigo in tramo:
        try:
            data_det = _mp_get({"codigo": codigo})
            time.sleep(SLEEP)
            if codigo not in set_nuevas:
                procesadas_refresh += 1
            lics = data_det.get("Listado") or []
            if not lics: continue
            lic = lics[0]
            cab, comp_row, fechas_row, item_rows, adj_rows = _parse(lic)

            # Si ya está en BD y el hash no cambió, no reescribimos (ahorra writes)
            prev_hash = hashes.get(codigo)
            if prev_hash and prev_hash == cab["raw_hash"]:
                sin_cambio += 1
                continue

            # El raw_hash es el testigo de "cabecera + hijos escritos", asi que se
            # estampa al final: si el upsert de items falla, la proxima corrida
            # reintenta en vez de darla por sincronizada.
            nuevo_hash = cab.pop("raw_hash")
            _sb_upsert(T_LIC,    "codigo_externo", [cab])
            _sb_upsert(T_COMP,   "codigo_externo", [comp_row])
            _sb_upsert(T_FECHAS, "codigo_externo", [fechas_row])
            if item_rows: _sb_upsert(T_ITEMS, "codigo_externo,correlativo", item_rows)
            if adj_rows:  _sb_upsert(T_ADJ, "licitacion_id,item_no,proveedor_rut", adj_rows)
            _sb_upsert(T_LIC, "codigo_externo",
                       [{"codigo_externo": codigo, "raw_hash": nuevo_hash}])

            if codigo in hashes: actualizadas += 1
            else:                nuevas += 1
            ok += 1

        except RateLimited as e:
            # Cuota del ticket agotada: seguir pidiendo solo gasta la ventana y
            # deja licitaciones saltadas. Se corta y el cursor avanza únicamente
            # sobre lo que alcanzó a procesarse.
            log.error("Cuota MP agotada en %s (%s) — se corta la corrida", codigo, e)
            corte_cuota = True
            break
        except Exception as e:
            log.warning("Error %s: %s", codigo, repr(e))
            err += 1

    # El cursor solo avanza sobre el refresco efectivamente recorrido. Si la
    # corrida se cortó por cuota, lo pendiente se retoma en la siguiente en vez
    # de quedar saltado hasta la próxima vuelta completa.
    if corte_cuota:
        nuevo_cursor = (cursor + procesadas_refresh) % total
    _save_cursor(nuevo_cursor, total, max(0, len(nuevas_pend) - nuevas))
    log.info("Resultado: %d nuevas, %d actualizadas, %d sin cambio, %d errores | "
             "cursor %d→%d de %d | quedan %d sin ingestar",
             nuevas, actualizadas, sin_cambio, err, cursor, nuevo_cursor, total,
             max(0, len(nuevas_pend) - nuevas))
    if corte_cuota:
        sys.exit(1)

if __name__ == "__main__":
    main()
