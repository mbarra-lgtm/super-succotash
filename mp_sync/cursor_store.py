"""
cursor_store.py
===============
Persistencia de cursores en Supabase (tabla mp_sync_cursor) para correr en
runners efímeros (GitHub Actions) donde los archivos .cursor_*.json no sobreviven.

Uso:
    from cursor_store import load_cursor, save_cursor
    data = load_cursor("activas", default={})      # dict
    save_cursor("activas", {"date": "...", "pos": 100})

Si Supabase no responde, hace fallback a un archivo local .cursor_<key>.json
(útil para correr a mano sin red). Set CURSOR_BACKEND=file para forzar archivo.
"""

import os, json, logging, requests

log = logging.getLogger("cursor_store")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY       = os.environ.get("SUPABASE_SERVICE_KEY", "")
SB_REST      = f"{SUPABASE_URL}/rest/v1"
BACKEND      = os.getenv("CURSOR_BACKEND", "supabase")  # "supabase" | "file"
T_CURSOR     = "mp_sync_cursor"

def _headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}

def _file_path(key: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f".cursor_{key}.json")

def _file_load(key, default):
    try:
        p = _file_path(key)
        if os.path.exists(p):
            return json.loads(open(p, encoding="utf-8").read())
    except Exception as e:
        log.warning("cursor file load %s: %s", key, e)
    return default

def _file_save(key, value):
    try:
        json.dump(value, open(_file_path(key), "w", encoding="utf-8"))
    except Exception as e:
        log.warning("cursor file save %s: %s", key, e)

class CursorError(RuntimeError):
    """No se pudo leer o guardar el cursor en Supabase."""


def _usar_archivo() -> bool:
    return BACKEND == "file" or not SUPABASE_URL or not SB_KEY


def load_cursor(key: str, default=None):
    """Lee el cursor. Levanta si Supabase responde mal.

    Antes, un error HTTP devolvía `default` en silencio: para sync_activas eso
    significa arrancar el round-robin desde la posición 0, y el barrido nunca
    llega a la cola de la lista. Un cursor que se pierde sin ruido es peor que
    una corrida que falla, porque el daño no se ve.
    """
    if default is None:
        default = {}
    if _usar_archivo():
        return _file_load(key, default)

    r = requests.get(f"{SB_REST}/{T_CURSOR}", headers=_headers(),
                     params={"select": "value", "key": f"eq.{key}", "limit": "1"},
                     timeout=20)
    if not r.ok:
        raise CursorError(f"load {key}: HTTP {r.status_code} — {r.text[:150]}")
    filas = r.json()
    # Sin fila es legítimo: es la primera corrida de esa clave.
    return filas[0]["value"] if filas else default


def save_cursor(key: str, value):
    """Guarda el cursor. Levanta si no se pudo — NO cae a un archivo local.

    El fallback a `.cursor_<key>.json` era una trampa en GitHub Actions: el
    runner se destruye al terminar el job y el archivo se va con él, así que el
    cursor quedaba clavado en su último valor bueno mientras el log decía
    "fallback archivo" en un WARNING que nadie mira. El cursor de compra ágil
    estuvo así 9 días. El archivo sigue disponible con CURSOR_BACKEND=file, que
    es cuando de verdad tiene sentido (correr a mano, sin red).
    """
    if _usar_archivo():
        return _file_save(key, value)

    from datetime import datetime, timezone
    r = requests.post(f"{SB_REST}/{T_CURSOR}", headers=_headers(),
                      params={"on_conflict": "key"},
                      json=[{"key": key, "value": value,
                             "updated_at": datetime.now(timezone.utc).isoformat()}],
                      timeout=20)
    if not r.ok:
        raise CursorError(f"save {key}: HTTP {r.status_code} — {r.text[:150]}")
