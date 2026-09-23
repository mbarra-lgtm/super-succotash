#!/usr/bin/env python3
"""
sync_ssa_supabase.py — API de consulta de SSA (v1) → Supabase.

QUÉ TRAE. El avance de TRABAJO por OF (actividades y asignaciones de SSA),
que el espejo de Odoo no tiene: ahí el "avance" es consumo de material.
Aterriza en tres tablas del esquema public:

    ssa_ofs            una fila por OF (llave of_odoo_id) con los totales que
                       calcula SSA. No se recalculan aquí a propósito: el
                       avance oficial es el de SSA (ver API_2.md).
    ssa_actividades    una fila por actividad (llave actividad_odoo_id)
    ssa_asignaciones   una fila por asignación (llave asignacion_key)

Cada fila guarda el JSON completo en `raw`: la API puede AGREGAR campos dentro
de v1 y no queremos perderlos ni rompernos por ellos.

MODOS
    incremental (default)  /ofs?modificadas_desde=<watermark - 1 día>, y para
                           ESAS OFs vuelve a pedir actividades y asignaciones.
                           Borra las filas de esas OFs que ya no vinieron
                           (actividad cancelada, asignación eliminada).
    --full / SSA_FULL=1    trae todo sin filtro y borra lo que no vino.
                           Pensado para 1x/día; corrige cualquier deriva.
    --probe / SSA_PROBE=1  no escribe nada: imprime la forma de la respuesta de
                           cada endpoint (llaves y 1 fila de ejemplo sin
                           colaboradores). Correr esto primero.

WATERMARK. Se lee de data_freshness('ssa_ofs'). Se pide desde un día antes
porque el filtro documentado es por fecha, y re-traer un día es idempotente.

SUPUESTO A VIGILAR. El incremental asume que cuando cambia una actividad o una
asignación, SSA marca su OF como modificada (sus totales cambian, así que
debería). Si el probe o la práctica muestran lo contrario, el full diario lo
corrige igual, con hasta un día de atraso.

ENTORNO
    SSA_API_KEY                       (secret) clave del consumidor
    SSA_BASE_URL                      default https://ssa.bertonati.cl/api/v1
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_SERVICE_KEY)
    SSA_OF_BATCH                      OFs por request en incremental (default 40)
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

BASE = os.getenv("SSA_BASE_URL", "https://ssa.bertonati.cl/api/v1").rstrip("/")
API_KEY = os.environ.get("SSA_API_KEY", "")
PROBE = "--probe" in sys.argv or os.getenv("SSA_PROBE") == "1"
FULL = "--full" in sys.argv or os.getenv("SSA_FULL") == "1"
OF_BATCH = int(os.getenv("SSA_OF_BATCH", "40"))
PAGE = 500  # máximo que acepta la API

T_OFS, T_ACT, T_ASG = "ssa_ofs", "ssa_actividades", "ssa_asignaciones"
DATASET = "ssa_ofs"

if not API_KEY:
    sys.exit("Falta SSA_API_KEY (cárgala en GitHub → Settings → Secrets → Actions).")


# ── Cliente SSA ─────────────────────────────────────────────────────────────
class SSA:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({"X-API-Key": API_KEY, "Accept": "application/json",
                               "User-Agent": "super-succotash/ssa_sync"})
        self.calls = 0

    def get(self, path: str, params: Optional[dict] = None, retries: int = 5) -> Any:
        url = f"{BASE}{path}"
        for i in range(retries):
            try:
                r = self.s.get(url, params=params, timeout=60)
                self.calls += 1
                if r.status_code == 401:
                    sys.exit("SSA respondió 401: la clave no existe o fue revocada.")
                if r.status_code in (429, 502, 503, 504):
                    raise requests.HTTPError(f"{r.status_code} transitorio")
                r.raise_for_status()
                return r.json()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                if i == retries - 1:
                    raise
                wait = 2 ** i
                print(f"  ↻ {path} {e} — reintento en {wait}s")
                time.sleep(wait)

    @staticmethod
    def items_of(payload: Any) -> Tuple[List[dict], Optional[int]]:
        """La doc garantiza `total` pero no nombra la lista: se toma la primera
        lista de objetos del cuerpo."""
        if isinstance(payload, list):
            return payload, None
        total = payload.get("total")
        for k in ("items", "datos", "data", "resultados", "results", "filas"):
            if isinstance(payload.get(k), list):
                return payload[k], total
        for v in payload.values():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v, total
        return [], total

    def paged(self, path: str, params: Optional[dict] = None) -> Iterable[dict]:
        params = dict(params or {})
        offset = 0
        while True:
            params.update({"limite": PAGE, "offset": offset})
            rows, total = self.items_of(self.get(path, params))
            yield from rows
            offset += len(rows)
            if not rows or len(rows) < PAGE or (total is not None and offset >= total):
                break


# ── Mapeo ───────────────────────────────────────────────────────────────────
def pick(row: dict, *keys: str) -> Any:
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return None

def num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None

def as_int(v: Any) -> Optional[int]:
    try:
        return None if v in (None, "") else int(v)
    except (TypeError, ValueError):
        return None

OF_NAME_KEYS = ("of", "nombre", "of_nombre", "name", "codigo")

def map_of(r: dict, run_ts: str) -> Optional[dict]:
    of_id = as_int(r.get("of_odoo_id"))
    if of_id is None:
        return None
    return {
        "of_odoo_id": of_id,
        "of_nombre": pick(r, *OF_NAME_KEYS),
        "of_primaria_odoo_id": as_int(r.get("of_primaria_odoo_id")),
        "nota_venta_odoo_id": as_int(r.get("nota_venta_odoo_id")),
        "lead_odoo_id": as_int(r.get("lead_odoo_id")),
        "cliente_odoo_id": as_int(r.get("cliente_odoo_id")),
        "estado": pick(r, "estado", "state"),
        "actividades": as_int(r.get("actividades")),
        "actividades_abiertas": as_int(r.get("actividades_abiertas")),
        "horas_planificadas": num(r.get("horas_planificadas")),
        "uet": num(r.get("uet")),
        "asignaciones": as_int(r.get("asignaciones")),
        "asignaciones_abiertas": as_int(r.get("asignaciones_abiertas")),
        "unidades_reconocidas": num(r.get("unidades_reconocidas")),
        "unidades_comprometidas": num(r.get("unidades_comprometidas")),
        "unidades_totales": num(r.get("unidades_totales")),
        "avance_pct": num(r.get("avance_pct")),
        "raw": r,
        "synced_at": run_ts,
    }

def map_act(r: dict, run_ts: str) -> Optional[dict]:
    act_id = as_int(r.get("actividad_odoo_id"))
    if act_id is None:
        return None
    return {
        "actividad_odoo_id": act_id,
        "of_odoo_id": as_int(r.get("of_odoo_id")),
        "centro_odoo_id": as_int(r.get("centro_odoo_id")),
        "nombre": pick(r, "nombre", "actividad", "name"),
        "estado": pick(r, "estado", "state"),
        "no_planificada": bool(r.get("no_planificada")) if r.get("no_planificada") is not None else None,
        "np_regularizado": bool(r.get("np_regularizado")) if r.get("np_regularizado") is not None else None,
        "raw": r,
        "synced_at": run_ts,
    }

ASG_ID_KEYS = ("asignacion_id", "id", "uuid", "asignacion_uuid")

def map_asg(r: dict, run_ts: str, act_to_of: Dict[int, int]) -> dict:
    key = pick(r, *ASG_ID_KEYS)
    if key is None:  # llave estable derivada si la API no expone id propio
        basis = [r.get("actividad_odoo_id"), r.get("colaborador_odoo_id"),
                 pick(r, "emitida", "emitida_en", "fecha_emision", "fecha_asignacion")]
        key = "h:" + hashlib.sha1(json.dumps(basis, default=str).encode()).hexdigest()[:24]
    act_id = as_int(r.get("actividad_odoo_id"))
    of_id = as_int(r.get("of_odoo_id")) or (act_to_of.get(act_id) if act_id else None)
    return {
        "asignacion_key": str(key),
        "actividad_odoo_id": act_id,
        "of_odoo_id": of_id,
        "colaborador_odoo_id": as_int(r.get("colaborador_odoo_id")),
        "estado": pick(r, "estado", "state"),
        "raw": r,
        "synced_at": run_ts,
    }


# ── Probe ───────────────────────────────────────────────────────────────────
def redact(r: dict) -> dict:
    """El probe queda en el log de Actions (repo público): sin nombres de personas."""
    out = {}
    for k, v in r.items():
        low = k.lower()
        out[k] = "<omitido>" if any(t in low for t in ("colaborador", "nombre_c", "operario", "persona", "codigo_interno")) and not low.endswith("odoo_id") else v
    return out

def probe(api: SSA) -> None:
    print("salud:", api.get("/salud"))
    for path, extra in (("/ofs", {}), ("/actividades", {}), ("/asignaciones", {})):
        payload = api.get(path, {"limite": 2, **extra})
        rows, total = api.items_of(payload)
        env_keys = list(payload.keys()) if isinstance(payload, dict) else "lista"
        print(f"\n== {path}  total={total}  sobre={env_keys}")
        if rows:
            print("llaves:", sorted(rows[0].keys()))
            print("ejemplo:", json.dumps(redact(rows[0]), ensure_ascii=False, default=str)[:1500])
    print(f"\nllamadas: {api.calls}. Nada se escribió en Supabase.")


# ── Supabase ────────────────────────────────────────────────────────────────
def sb_client():
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)

def chunked(xs: List[Any], n: int) -> Iterable[List[Any]]:
    for i in range(0, len(xs), n):
        yield xs[i:i + n]

def upsert(sb, table: str, rows: List[dict], conflict: str) -> int:
    # dedup por llave: la paginación con offset puede repetir una fila si algo
    # cambia entre páginas, y un upsert con llave repetida falla entero
    uniq = {r[conflict]: r for r in rows}
    for part in chunked(list(uniq.values()), 500):
        sb.table(table).upsert(part, on_conflict=conflict).execute()
    return len(uniq)

def read_watermark(sb) -> Optional[datetime]:
    res = sb.table("data_freshness").select("refreshed_at").eq("dataset", DATASET).limit(1).execute()
    if res.data and res.data[0].get("refreshed_at"):
        return datetime.fromisoformat(res.data[0]["refreshed_at"].replace("Z", "+00:00"))
    return None

def stamp(sb, rows_changed: int, ms: int, source: str) -> None:
    sb.table("data_freshness").upsert({
        "dataset": DATASET, "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "rows_changed": rows_changed, "duration_ms": ms, "source": source,
    }, on_conflict="dataset").execute()

def count(sb, table: str) -> int:
    return sb.table(table).select("*", count="exact", head=True).execute().count or 0


# ── Corridas ────────────────────────────────────────────────────────────────
def run_full(api: SSA, sb, run_ts: str) -> int:
    ofs = [m for r in api.paged("/ofs", {"incluir_canceladas": "true"}) if (m := map_of(r, run_ts))]
    acts = [m for r in api.paged("/actividades") if (m := map_act(r, run_ts))]
    act_to_of = {a["actividad_odoo_id"]: a["of_odoo_id"] for a in acts if a["of_odoo_id"]}
    asgs = [map_asg(r, run_ts, act_to_of) for r in api.paged("/asignaciones")]

    n = upsert(sb, T_OFS, ofs, "of_odoo_id") + upsert(sb, T_ACT, acts, "actividad_odoo_id") \
        + upsert(sb, T_ASG, asgs, "asignacion_key")
    print(f"full: {len(ofs)} OFs · {len(acts)} actividades · {len(asgs)} asignaciones")

    # Borrado de lo que no vino. Guarda: si una tabla llegó sospechosamente
    # vacía o chica (API caída a medias), no se borra nada de esa tabla.
    for table, got in ((T_OFS, len(ofs)), (T_ACT, len(acts)), (T_ASG, len(asgs))):
        before = count(sb, table)
        if got == 0 or (before and got < 0.5 * before):
            print(f"⚠️ {table}: llegaron {got} contra {before} existentes — no borro")
            continue
        sb.table(table).delete().lt("synced_at", run_ts).execute()
    return n

def run_incremental(api: SSA, sb, run_ts: str) -> int:
    wm = read_watermark(sb)
    if wm is None:
        print("sin watermark previo → corre full")
        return run_full(api, sb, run_ts)
    desde = (wm.astimezone(ZoneInfo("America/Santiago")) - timedelta(days=1)).date().isoformat()
    ofs = [m for r in api.paged("/ofs", {"modificadas_desde": desde, "incluir_canceladas": "true"})
           if (m := map_of(r, run_ts))]
    print(f"incremental desde {desde}: {len(ofs)} OFs modificadas")
    if not ofs:
        return 0
    n = upsert(sb, T_OFS, ofs, "of_odoo_id")

    CANCEL = ("cancel", "cancelada", "anulada")
    vivas = [o for o in ofs if (o["estado"] or "").lower() not in CANCEL]
    muertas = [o["of_odoo_id"] for o in ofs if (o["estado"] or "").lower() in CANCEL]
    if muertas:  # OF cancelada: su trabajo deja de contar
        for part in chunked(muertas, 100):
            sb.table(T_ASG).delete().in_("of_odoo_id", part).execute()
            sb.table(T_ACT).delete().in_("of_odoo_id", part).execute()

    for batch in chunked(vivas, OF_BATCH):
        ids = [o["of_odoo_id"] for o in batch]
        acts = [m for r in api.paged("/actividades", {"of_odoo_id": ",".join(map(str, ids))})
                if (m := map_act(r, run_ts))]
        act_to_of = {a["actividad_odoo_id"]: a["of_odoo_id"] for a in acts if a["of_odoo_id"]}
        nombres = [o["of_nombre"] for o in batch if o["of_nombre"]]
        if len(nombres) == len(batch):
            asgs = [map_asg(r, run_ts, act_to_of) for r in api.paged("/asignaciones", {"of": ",".join(nombres)})]
        else:  # sin nombre de OF no hay filtro por OF en /asignaciones: se va por lead
            leads = sorted({o["lead_odoo_id"] for o in batch if o["lead_odoo_id"]})
            asgs = [map_asg(r, run_ts, act_to_of) for r in api.paged("/asignaciones", {"lead_odoo_id": ",".join(map(str, leads))})] if leads else []
            asgs = [a for a in asgs if a["of_odoo_id"] in ids]
        for a in acts:  # la actividad puede no traer of_odoo_id si viene implícito en el filtro
            a["of_odoo_id"] = a["of_odoo_id"] or (ids[0] if len(ids) == 1 else None)
        n += upsert(sb, T_ACT, acts, "actividad_odoo_id") + upsert(sb, T_ASG, asgs, "asignacion_key")
        # lo que no volvió para estas OFs ya no existe
        sb.table(T_ACT).delete().in_("of_odoo_id", ids).lt("synced_at", run_ts).execute()
        sb.table(T_ASG).delete().in_("of_odoo_id", ids).lt("synced_at", run_ts).execute()
        print(f"  lote {ids[0]}…: {len(acts)} actividades · {len(asgs)} asignaciones")
    return n


def main() -> None:
    api = SSA()
    if PROBE:
        probe(api)
        return
    print("salud:", api.get("/salud"))
    sb = sb_client()
    t0 = time.time()
    run_ts = datetime.now(timezone.utc).isoformat()
    n = run_full(api, sb, run_ts) if FULL else run_incremental(api, sb, run_ts)
    ms = int((time.time() - t0) * 1000)
    stamp(sb, n, ms, "ssa_api_v1:" + ("full" if FULL else "incremental"))
    print(f"✓ {n} filas en {ms/1000:.1f}s · {api.calls} llamadas a SSA")


if __name__ == "__main__":
    main()
