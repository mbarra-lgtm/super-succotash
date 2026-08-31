"""
sb.py
=====
Acceso a Supabase (PostgREST) para el radar CORE. Mismo patrón que
cursor_store.py de mp_sync: REST en vez de conexión Postgres directa,
porque en runners efímeros el pooler da problemas de IPv6 y timeouts.

Uso:
    import sb
    fuentes = sb.select("core_sources", {"select": "*", "enabled": "is.true"})
    sb.insert("core_documents", filas, on_conflict="document_url", ignorar_dup=True)
    clave = sb.rpc("fn_core_dedupe_key", {...})
"""

import os, time, logging, requests

log = logging.getLogger("sb")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"
TIMEOUT      = int(os.getenv("SB_TIMEOUT", "60"))

_session = requests.Session()


def _headers(prefer: str | None = None) -> dict:
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
         "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


def _req(method: str, path: str, prefer: str | None = None, **kw):
    """3 intentos con backoff. Los 4xx no se reintentan: son error nuestro."""
    url = f"{SB_REST}/{path.lstrip('/')}"
    ultimo = None
    for intento in range(3):
        try:
            r = _session.request(method, url, headers=_headers(prefer),
                                 timeout=TIMEOUT, **kw)
            if 400 <= r.status_code < 500:
                raise RuntimeError(f"{r.status_code}: {r.text[:400]}")
            if r.status_code >= 500:
                ultimo = RuntimeError(f"{r.status_code}: {r.text[:200]}")
                time.sleep(2 ** intento)
                continue
            return r.json() if r.content else None
        except RuntimeError:
            raise
        except Exception as e:
            ultimo = e
            time.sleep(2 ** intento)
    raise RuntimeError(f"Supabase no responde tras 3 intentos: {ultimo}")


# ── API ─────────────────────────────────────────────────────────────────────

def select(tabla: str, params: dict) -> list:
    return _req("GET", tabla, params=params) or []


def insert(tabla: str, filas: list, on_conflict: str | None = None,
           ignorar_dup: bool = False) -> list:
    if not filas:
        return []
    prefer = ["return=representation"]
    params = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
        prefer.append("resolution=ignore-duplicates" if ignorar_dup
                      else "resolution=merge-duplicates")
    return _req("POST", tabla, prefer=",".join(prefer),
                params=params, json=filas) or []


def update(tabla: str, filtro: dict, cambios: dict) -> None:
    _req("PATCH", tabla, prefer="return=minimal", params=filtro, json=cambios)


def rpc(funcion: str, args: dict):
    return _req("POST", f"rpc/{funcion}", json=args)


# ── Helpers de dominio ──────────────────────────────────────────────────────

def fuentes_pendientes(slug: str | None = None) -> list:
    """Fuentes habilitadas cuya frecuencia_horas ya venció."""
    from datetime import datetime, timezone

    params = {
        "select": "id,slug,nombre,region,comuna,organismo_id,fuente_tipo,parser,"
                  "landing_url,config,frecuencia_horas,rate_limit_ms,"
                  "last_checked_at,consecutive_errors",
        "enabled": "is.true",
        "order": "last_checked_at.asc.nullsfirst",
    }
    if slug:
        params["slug"] = f"eq.{slug}"
        return select("core_sources", params)

    ahora = datetime.now(timezone.utc)
    pendientes = []
    for f in select("core_sources", params):
        visto = f.get("last_checked_at")
        if not visto:
            pendientes.append(f); continue
        t = datetime.fromisoformat(visto.replace("Z", "+00:00"))
        if (ahora - t).total_seconds() >= f["frecuencia_horas"] * 3600:
            pendientes.append(f)
    return pendientes


def marcar_fuente_ok(source_id: int, documentos_nuevos: int) -> None:
    from datetime import datetime, timezone
    ahora = datetime.now(timezone.utc).isoformat()
    cambios = {"last_checked_at": ahora, "last_ok_at": ahora,
               "consecutive_errors": 0, "last_error": None}
    if documentos_nuevos:
        cambios["last_document_at"] = ahora
    update("core_sources", {"id": f"eq.{source_id}"}, cambios)


def marcar_fuente_error(source_id: int, errores_previos: int, msg: str) -> None:
    from datetime import datetime, timezone
    ahora = datetime.now(timezone.utc).isoformat()
    update("core_sources", {"id": f"eq.{source_id}"}, {
        "last_checked_at": ahora, "last_error_at": ahora,
        "last_error": msg[:500], "consecutive_errors": (errores_previos or 0) + 1,
    })


def urls_ya_vistas(source_id: int) -> set:
    filas = select("core_documents",
                   {"select": "document_url", "source_id": f"eq.{source_id}"})
    return {f["document_url"] for f in filas}
