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

def load_cursor(key: str, default=None):
    if default is None:
        default = {}
    if BACKEND == "file" or not SUPABASE_URL or not SB_KEY:
        return _file_load(key, default)
    try:
        r = requests.get(f"{SB_REST}/{T_CURSOR}", headers=_headers(),
                         params={"select": "value", "key": f"eq.{key}", "limit": "1"},
                         timeout=20)
        if r.ok and r.json():
            return r.json()[0]["value"]
        return default
    except Exception as e:
        log.warning("cursor supabase load %s: %s — fallback archivo", key, e)
        return _file_load(key, default)

def save_cursor(key: str, value):
    if BACKEND == "file" or not SUPABASE_URL or not SB_KEY:
        return _file_save(key, value)
    try:
        from datetime import datetime, timezone
        r = requests.post(f"{SB_REST}/{T_CURSOR}", headers=_headers(),
                          params={"on_conflict": "key"},
                          json=[{"key": key, "value": value,
                                 "updated_at": datetime.now(timezone.utc).isoformat()}],
                          timeout=20)
        if not r.ok:
            log.warning("cursor supabase save %s: %s — fallback archivo", key, r.text[:150])
            _file_save(key, value)
    except Exception as e:
        log.warning("cursor supabase save %s: %s — fallback archivo", key, e)
        _file_save(key, value)
