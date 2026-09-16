"""
sb_client.py
============
Cliente único de Supabase para los scripts de mp_sync.

Por qué existe
--------------
Había nueve copias de `_sb_upsert`, siete de ellas con esta forma:

    if not r.ok: log.error("SB %s: %s", table, r.text[:200])

Es decir: la escritura falla, se anota en el log, y el script sigue como si nada
— avanza cursores, estampa `data_freshness` y termina con código 0. Un corte de
Supabase de treinta segundos se convierte así en un hueco de datos permanente
que nadie ve. El 16-09-2026 un backfill estuvo seis horas golpeando una base
caída, con 740 respuestas 522, sin fallar ni una sola vez.

Este módulo impone tres reglas:

  1. Todo error transitorio (429, 5xx, 520/522/524 de Cloudflare, corte de red)
     se reintenta con backoff exponencial antes de rendirse.
  2. Si aun así no se logra, se LEVANTA. Nunca se devuelve silencio.
  3. Todo fallo queda contabilizado, para que el proceso pueda terminar con
     código ≠ 0 y el workflow se ponga rojo.

Los 4xx que no son 429 no se reintentan: un payload o un esquema mal formados no
mejoran esperando.

Uso típico
----------
    import sb_client as sb

    sb.upsert("mp_licitaciones", "codigo_externo", filas)
    existentes = sb.select_in("mp_licitaciones", "codigo_externo,raw_hash",
                              "codigo_externo", codigos)
    sb.stamp_freshness("mp_oc", filas_cambiadas, "sync_oc.py")
    sb.exit_si_hubo_fallos()          # al final de main()

Variables de entorno
--------------------
    SUPABASE_URL, SUPABASE_SERVICE_KEY   (obligatorias)
    SB_INTENTOS      reintentos por request        (default 5)
    SB_BACKOFF       factor de backoff en segundos (default 2.0)
    SB_TIMEOUT       timeout por request           (default 60)
    SB_CHUNK_FILAS   filas por POST                (default 500)
    SB_CHUNK_IN      valores por filtro in.(...)   (default 200)
"""

import os, time, random, logging, threading
from typing import Iterable, Optional

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:                                  # urllib3 < 1.26
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

log = logging.getLogger("sb_client")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"

INTENTOS   = int(os.getenv("SB_INTENTOS", "5"))
BACKOFF    = float(os.getenv("SB_BACKOFF", "2.0"))
TIMEOUT    = float(os.getenv("SB_TIMEOUT", "60"))
CHUNK_FILAS = int(os.getenv("SB_CHUNK_FILAS", "500"))
# El filtro in.(...) viaja en la URL: con listas largas se pasa del largo máximo
# y PostgREST devuelve vacío en vez de error, lo que hace ver como "no existe"
# algo que sí está. Trocear no es optimización, es corrección.
CHUNK_IN   = int(os.getenv("SB_CHUNK_IN", "200"))
# Cortacircuitos. Los bucles de los scripts atrapan Exception por item y siguen:
# contra una base caída eso se traduce en horas de martilleo inútil (el
# 16-09-2026: seis horas y 740 respuestas 522 sin que el job fallara nunca).
# Pasado este número de fallos SEGUIDOS se aborta el proceso completo.
MAX_FALLOS_SEGUIDOS = int(os.getenv("SB_MAX_FALLOS_SEGUIDOS", "10"))

# Estados que vale la pena reintentar. 520/522/524 son de Cloudflare: el origen
# (PostgREST) no respondió. 429 es rate limit del pooler.
REINTENTABLES = frozenset({429, 500, 502, 503, 504, 520, 522, 524})


class SupabaseError(RuntimeError):
    """Un request a Supabase no pudo completarse tras agotar los reintentos."""


# ── Contador de fallos ───────────────────────────────────────────────────────
# Un script puede decidir seguir adelante tras un fallo puntual (p. ej. el
# enriquecimiento de un registro), pero el proceso completo NO puede terminar
# en verde si hubo escrituras perdidas.
_lock = threading.Lock()
_fallos: list = []
_seguidos = 0


def registrar_fallo(detalle: str) -> None:
    """Contabiliza un fallo y abre el cortacircuitos si se repiten sin éxitos."""
    global _seguidos
    with _lock:
        _fallos.append(detalle)
        _seguidos += 1
        abrir = _seguidos >= MAX_FALLOS_SEGUIDOS

    if abrir:
        # SystemExit hereda de BaseException, no de Exception: así atraviesa los
        # `except Exception` de los bucles por item sin tener que tocarlos uno a
        # uno. Es deliberado — es la única forma de cortar desde acá.
        log.error("CORTACIRCUITOS: %d fallos seguidos de Supabase. "
                  "Se aborta la corrida en vez de seguir golpeando la base.", _seguidos)
        raise SystemExit(1)


def _registrar_exito() -> None:
    global _seguidos
    with _lock:
        _seguidos = 0


def fallos() -> list:
    with _lock:
        return list(_fallos)


def hubo_fallos() -> bool:
    with _lock:
        return bool(_fallos)


def exit_si_hubo_fallos(codigo: int = 1) -> None:
    """Termina el proceso en rojo si alguna operación no se pudo completar.

    Llamar al final de main(). Sin esto el workflow queda verde aunque se hayan
    perdido escrituras, que es precisamente lo que hacía invisibles los huecos.
    """
    pendientes = fallos()
    if pendientes:
        log.error("%d operación(es) de Supabase fallaron en esta corrida:", len(pendientes))
        for d in pendientes[:10]:
            log.error("  · %s", d)
        if len(pendientes) > 10:
            log.error("  · … y %d más", len(pendientes) - 10)
        raise SystemExit(codigo)


# ── Sesión HTTP ──────────────────────────────────────────────────────────────
def _construir_sesion() -> requests.Session:
    s = requests.Session()
    reintentos = Retry(
        total=INTENTOS - 1,
        backoff_factor=BACKOFF,
        status_forcelist=sorted(REINTENTABLES),
        # Por defecto urllib3 solo reintenta métodos idempotentes y deja fuera
        # POST. Acá los POST son upserts con resolution=merge-duplicates: repetir
        # uno da exactamente el mismo resultado, así que reintentarlos es seguro
        # y es justo lo que hace falta (las escrituras son lo que se perdía).
        allowed_methods=False,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adaptador = HTTPAdapter(max_retries=reintentos, pool_maxsize=20)
    s.mount("https://", adaptador)
    s.mount("http://", adaptador)
    return s


_sesion = _construir_sesion()


def _headers(prefer: Optional[str] = None) -> dict:
    h = {
        "apikey": SB_KEY,
        "Authorization": f"Bearer {SB_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def request(metodo: str, table: str, *, prefer: Optional[str] = None,
            **kw) -> requests.Response:
    """Request a PostgREST con reintentos; levanta SupabaseError si no se logra.

    El Retry del adaptador cubre los cortes de conexión y los status de
    REINTENTABLES. Este bucle externo agrega una segunda capa con jitter, que es
    lo que separa a los runners concurrentes cuando la base vuelve de una caída
    y todos reintentan al mismo tiempo.
    """
    url = f"{SB_REST}/{table}"
    kw.setdefault("timeout", TIMEOUT)
    ultimo = None

    for intento in range(1, INTENTOS + 1):
        try:
            r = _sesion.request(metodo, url, headers=_headers(prefer), **kw)
        except requests.RequestException as e:
            ultimo = repr(e)
        else:
            if r.ok:
                _registrar_exito()
                return r
            ultimo = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in REINTENTABLES:
                # 4xx de contenido: el payload o el esquema están mal.
                detalle = f"{metodo} {table} — {ultimo}"
                registrar_fallo(detalle)
                raise SupabaseError(detalle)

        if intento < INTENTOS:
            espera = min(60.0, BACKOFF * (2 ** (intento - 1))) + random.uniform(0, 2)
            log.warning("SB %s %s (%s) — reintento %d/%d en %.0fs",
                        metodo, table, ultimo, intento, INTENTOS - 1, espera)
            time.sleep(espera)

    detalle = f"{metodo} {table} falló tras {INTENTOS} intentos — {ultimo}"
    registrar_fallo(detalle)
    raise SupabaseError(detalle)


# ── Operaciones ──────────────────────────────────────────────────────────────
def upsert(table: str, on_conflict: str, rows: list, *,
           chunk: int = CHUNK_FILAS, devolver: bool = False):
    """Inserta o actualiza filas. Levanta si alguna tanda no se pudo escribir.

    Devuelve la cantidad de filas efectivamente escritas — no la cantidad que se
    le pasó, que es lo que hacía `sync_oc._sb_upsert` y volvía mentirosos tanto
    los contadores del log como el `rows_changed` de data_freshness.
    """
    if not rows:
        return [] if devolver else 0

    prefer = "resolution=merge-duplicates," + ("return=representation" if devolver
                                               else "return=minimal")
    params = {"on_conflict": on_conflict} if on_conflict else {}
    escritas, salida = 0, []

    for i in range(0, len(rows), chunk):
        tanda = rows[i:i + chunk]
        r = request("POST", table, params=params, json=tanda, prefer=prefer)
        escritas += len(tanda)
        if devolver and r.content:
            salida.extend(r.json())

    return salida if devolver else escritas


def delete(table: str, filtros: dict) -> None:
    """Borra por filtro PostgREST, p. ej. {"codigo_oc": "eq.12345"}.

    Levanta si no se pudo. Las versiones anteriores ni miraban el código de
    respuesta: un DELETE que "pasó" seguido de un INSERT que falló dejaba filas
    borradas y sin reponer — pérdida real, no un hueco.
    """
    request("DELETE", table, params=filtros)


def select(table: str, params: dict) -> list:
    """SELECT que levanta ante error en vez de devolver [].

    Devolver lista vacía ante un fallo hace indistinguible "la base no responde"
    de "no hay datos", y los llamadores toman la segunda lectura: marcan cosas
    como inexistentes, no encuentran pendientes, y reportan éxito.
    """
    return request("GET", table, params=params).json()


def select_in(table: str, select_cols: str, col: str, valores: Iterable,
              extra: Optional[dict] = None, *, chunk: int = CHUNK_IN) -> list:
    """SELECT con filtro in.(...) troceado. Devuelve todas las filas o levanta.

    Si un solo trozo falla se aborta entero: devolver un resultado parcial es
    peor que no devolver nada, porque el llamador lo interpreta como "estas
    filas no existen" y las vuelve a crear o las da por nuevas.
    """
    valores = [str(v) for v in valores]
    if not valores:
        return []

    filas = []
    for i in range(0, len(valores), chunk):
        lote = valores[i:i + chunk]
        params = {
            "select": select_cols,
            col: f"in.({','.join(lote)})",
            "limit": str(len(lote) + 1),
        }
        if extra:
            params.update(extra)
        filas.extend(select(table, params))
    return filas


def stamp_freshness(dataset: str, rows_changed: Optional[int] = None,
                    source: str = "", *, forzar: bool = False) -> bool:
    """Estampa el latido de frescura del dataset.

    Por defecto NO estampa si hubo fallos en la corrida: `data_freshness` es el
    mecanismo con el que el panel decide si un dato está fresco, y estamparlo
    tras una corrida rota es cómo un pipeline caído se ve verde durante días.
    `forzar=True` solo para casos donde el latido mide otra cosa.

    Devuelve True si estampó.
    """
    if hubo_fallos() and not forzar:
        log.warning("No se estampa data_freshness[%s]: la corrida tuvo %d fallo(s)",
                    dataset, len(fallos()))
        return False

    from datetime import datetime, timezone
    fila = {
        "dataset": dataset,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    if rows_changed is not None:
        fila["rows_changed"] = rows_changed

    try:
        upsert("data_freshness", "dataset", [fila])
        return True
    except SupabaseError as e:
        # El latido es observabilidad: que falle no debe tumbar la corrida, pero
        # sí queda contabilizado por `request`.
        log.warning("No pude estampar data_freshness[%s]: %s", dataset, e)
        return False
