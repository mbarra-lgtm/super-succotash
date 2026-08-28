#!/usr/bin/env python3
"""
resync_presupuestos.py — re-sincroniza SOLO los presupuestos analíticos.

POR QUÉ EXISTE. El sync completo tarda 20 minutos y `sync_odoo_supabase.py` no
tiene guarda `if __name__ == "__main__"`: importarlo arranca el loop infinito.
Los presupuestos son ~50 registros y tardan segundos, así que este script trae
su propio cliente mínimo y no toca nada más.

QUÉ CORRIGE. La primera versión asumió que `budget.line` guarda la cuenta
analítica en `analytic_distribution` (jsonb), como `purchase.order.line`.
Medido contra la instancia real: las 48 líneas llegaron con ese campo en NULL.
En este Odoo la cuenta viaja en campos many2one por plan analítico
(`x_plan<N>_id` o similar) — la columna "Plan Analítico Bertonati" de la ficha.

Este script NO adivina el nombre: pregunta por `fields_get` cuáles son los
many2one que apuntan a `account.analytic.account` y usa todos los que
encuentre. Además imprime el mapa de campos para que quede registro de contra
qué se sincronizó.

USO (mismas variables de entorno que el sync grande):
    ODOO_JSONRPC, ODOO_DB, ODOO_USER, ODOO_API_KEY,
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_SERVICE_KEY)

    python resync_presupuestos.py                    # sincroniza presupuestos
    python resync_presupuestos.py --dry-run          # solo inspecciona, no escribe
    python resync_presupuestos.py --backfill-costo-bom
        Rellena sales_notes.costo_bom_nv en TODAS las notas de venta.

BACKFILL: POR QUÉ HACE FALTA. `sync_sales_notes_incremental` filtra por
`write_date > watermark`, así que cuando se agregó la columna costo_bom_nv solo
la llenaron las NV modificadas después. Medido: 6 de 794. S01028 (Aduana de
Arica) tiene write_date del 31-jul y en Odoo su Costo BOM es $150.254.114 —
el dato existía, el sync nunca volvió a leer esa fila. Una columna nueva en un
sync incremental siempre necesita un backfill de una vez; después el
incremental la mantiene sola.
"""
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dateutil import parser as dtp
from supabase import create_client

ODOO_JSONRPC = os.environ["ODOO_JSONRPC"]
ODOO_DB      = os.environ["ODOO_DB"]
ODOO_USER    = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_SERVICE_KEY"]
ODOO_LANG    = os.getenv("ODOO_LANG", "es_CL")

DRY_RUN  = "--dry-run" in sys.argv
BACKFILL = "--backfill-costo-bom" in sys.argv

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Cliente Odoo mínimo ───────────────────────────────────────────────────
class Odoo:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})
        self.uid = self._call("common", "login", [ODOO_DB, ODOO_USER, ODOO_API_KEY])
        if not self.uid:
            raise RuntimeError("Login Odoo falló (uid vacío).")

    def _call(self, service: str, method: str, args: list, retries: int = 5) -> Any:
        payload = {"jsonrpc": "2.0", "method": "call",
                   "params": {"service": service, "method": method, "args": args}, "id": 1}
        last = None
        for i in range(retries):
            try:
                r = self.s.post(ODOO_JSONRPC, json=payload, timeout=180)
                r.raise_for_status()
                out = r.json()
                if out.get("error"):
                    raise RuntimeError(out["error"])
                return out.get("result")
            except Exception as e:
                last = e
                time.sleep(1.5 * (i + 1))
        raise RuntimeError(f"Odoo JSON-RPC falló: {last}")

    def execute_kw(self, model: str, method: str, args: list, kwargs: Optional[dict] = None) -> Any:
        kwargs = dict(kwargs or {})
        ctx = {"lang": ODOO_LANG}
        ctx.update(kwargs.get("context") or {})
        kwargs["context"] = ctx
        return self._call("object", "execute_kw",
                          [ODOO_DB, self.uid, ODOO_API_KEY, model, method, args, kwargs])

    def fields_get(self, model: str) -> Dict[str, Any]:
        return self.execute_kw(model, "fields_get", [],
                               {"attributes": ["string", "type", "relation"]})

    def search_read_all(self, model: str, domain: list, fields: list, chunk: int = 500):
        off = 0
        while True:
            batch = self.execute_kw(model, "search_read", [domain],
                                    {"fields": fields, "limit": chunk, "offset": off,
                                     "order": "id asc", "context": {"active_test": False}})
            if not batch:
                return
            yield batch
            off += len(batch)


# ── Helpers ───────────────────────────────────────────────────────────────
def m2o(v: Any) -> Tuple[Optional[int], Optional[str]]:
    if not v:
        return None, None
    if isinstance(v, list) and len(v) >= 2:
        return int(v[0]), str(v[1])
    if isinstance(v, list) and len(v) == 1:
        return int(v[0]), None
    if isinstance(v, int):
        return v, None
    return None, str(v)


def num(v: Any) -> Optional[float]:
    """False/'' → NULL. En un presupuesto 'sin dato' y 'cero' no son lo mismo."""
    if v is False or v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def pdate(v: Any) -> Optional[str]:
    if v is False or v is None or v == "":
        return None
    try:
        return dtp.parse(str(v)).date().isoformat()
    except Exception:
        return None


def pdt(v: Any) -> Optional[str]:
    if not v:
        return None
    d = dtp.parse(str(v))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


def avail(meta: Dict[str, Any], desired: List[str], model: str) -> List[str]:
    ok = [f for f in desired if f in meta]
    missing = [f for f in desired if f not in meta]
    if missing:
        print(f"⚠️  {model}: campos no disponibles, se omiten: {missing}")
    return ok


def upsert(func: str, rows: List[dict], size: int = 500) -> None:
    if not rows:
        return
    for i in range(0, len(rows), size):
        res = sb.rpc(func, {"payload": rows[i:i + size]}).execute()
        err = getattr(res, "error", None)
        if err:
            raise RuntimeError(f"Supabase RPC {func}: {err}")


# ── Detección del vínculo analítico en budget.line ───────────────────────
def campos_analiticos(meta: Dict[str, Any]) -> List[str]:
    """
    Todos los many2one de budget.line que apuntan a account.analytic.account.
    En Odoo 17/18 cada plan analítico genera el suyo (x_plan1_id, x_plan2_id…),
    así que puede haber más de uno y la cantidad cambia si alguien crea un plan.
    Por eso se descubren en vez de fijarse.
    """
    out = []
    for fname, fmeta in meta.items():
        if (fmeta.get("type") == "many2one"
                and fmeta.get("relation") == "account.analytic.account"):
            out.append(fname)
    return sorted(out)


_NV_COSTO_BOM_CANDIDATOS = [
    "x_studio_costo_bom",
    "x_studio_costo_bom_1",
    "x_studio_costo_de_bom",
    "x_studio_costo_lista_de_materiales",
]


def detectar_campo_costo_bom(meta: Dict[str, Any]) -> Optional[str]:
    """Mismo criterio que el sync grande: candidatos conocidos y, si ninguno
    existe, el primer x_studio_* numérico cuya etiqueta hable de costo y BOM."""
    for c in _NV_COSTO_BOM_CANDIDATOS:
        if c in meta:
            return c
    for fname, fm in meta.items():
        if not fname.startswith("x_studio_"):
            continue
        if (fm.get("type") or "") not in {"float", "monetary", "integer"}:
            continue
        et = f"{fname} {fm.get('string') or ''}".lower()
        if ("bom" in et or "materiales" in et) and "costo" in et:
            return fname
    return None


def backfill_costo_bom(odoo: Odoo) -> int:
    """
    Rellena sales_notes.costo_bom_nv leyendo TODAS las sale.order.

    Solo toca filas que YA existen en el espejo: un upsert con dos columnas
    sobre un odoo_id desconocido crearía una NV fantasma sin nombre ni estado.
    """
    meta = odoo.fields_get("sale.order")
    campo = detectar_campo_costo_bom(meta)
    if not campo:
        print("❌ No se encontró ningún x_studio_* numérico de costo/BOM en sale.order.")
        return 1
    print(f"ℹ️  Campo detectado: {campo} ({meta[campo].get('string')})")

    # ids que ya están en el espejo (paginado: PostgREST corta en 1000)
    existentes = set()
    desde = 0
    while True:
        r = (sb.table("sales_notes").select("odoo_id")
               .range(desde, desde + 999).execute())
        d = getattr(r, "data", None) or []
        if not d:
            break
        existentes.update(x["odoo_id"] for x in d)
        if len(d) < 1000:
            break
        desde += 1000
    print(f"ℹ️  NV en el espejo: {len(existentes)}")

    rows: List[dict] = []
    leidas = 0
    for batch in odoo.search_read_all("sale.order", [], ["id", campo], chunk=500):
        for r in batch:
            leidas += 1
            oid = int(r["id"])
            if oid not in existentes:
                continue
            rows.append({"odoo_id": oid, "costo_bom_nv": num(r.get(campo))})

    con_valor = sum(1 for r in rows if r["costo_bom_nv"] not in (None, 0))
    print(f"Leídas de Odoo: {leidas} · a actualizar: {len(rows)} · con valor > 0: {con_valor}")

    if DRY_RUN:
        print("--dry-run: no se escribió nada.")
        return 0

    for i in range(0, len(rows), 500):
        res = (sb.table("sales_notes")
                 .upsert(rows[i:i + 500], on_conflict="odoo_id")
                 .execute())
        err = getattr(res, "error", None)
        if err:
            raise RuntimeError(f"Supabase upsert sales_notes: {err}")
    print(f"✅ sales_notes.costo_bom_nv actualizado en {len(rows)} filas")
    return 0


def main() -> int:
    odoo = Odoo()

    if BACKFILL:
        return backfill_costo_bom(odoo)

    if not odoo.fields_get("budget.analytic"):
        print("❌ budget.analytic no existe en esta base.")
        return 1

    meta_head = odoo.fields_get("budget.analytic")
    meta_line = odoo.fields_get("budget.line")

    campos_an = campos_analiticos(meta_line)
    print("─" * 70)
    print("Campos de budget.line que apuntan a account.analytic.account:")
    if campos_an:
        for f in campos_an:
            print(f"   • {f}  ({meta_line[f].get('string')})")
    else:
        print("   (ninguno)")
    tiene_dist = "analytic_distribution" in meta_line
    print(f"analytic_distribution presente en budget.line: {tiene_dist}")
    print("─" * 70)

    if not campos_an and not tiene_dist:
        print("❌ budget.line no expone ni analytic_distribution ni un many2one a "
              "account.analytic.account. Sin eso no hay forma de vincular el "
              "presupuesto a una cuenta. Campos disponibles:")
        for fname, fmeta in sorted(meta_line.items()):
            print(f"   {fname:38s} {fmeta.get('type'):12s} {fmeta.get('relation') or ''}")
        return 1

    run_ts = datetime.now(timezone.utc).isoformat()

    # ---------- cabeceras ----------
    f_head = avail(meta_head, ["id", "name", "state", "budget_type", "date_from", "date_to",
                               "user_id", "company_id", "currency_id",
                               "budget_amount", "committed_amount", "achieved_amount",
                               "write_date"], "budget.analytic")
    heads: Dict[int, dict] = {}
    rows_head: List[dict] = []
    for batch in odoo.search_read_all("budget.analytic", [], f_head):
        for r in batch:
            uid_, uname = m2o(r.get("user_id"))
            cid, cname = m2o(r.get("company_id"))
            curid, curname = m2o(r.get("currency_id"))
            row = {
                "odoo_id": int(r["id"]),
                "name": (r.get("name") or "").strip() or None,
                "budget_type": r.get("budget_type") or None,
                "state": r.get("state") or None,
                "date_from": pdate(r.get("date_from")),
                "date_to": pdate(r.get("date_to")),
                "user_id": uid_, "user_name": uname,
                "company_id": cid, "company_name": cname,
                "currency_id": curid, "currency_name": curname,
                "budget_amount": num(r.get("budget_amount")),
                "committed_amount": num(r.get("committed_amount")),
                "achieved_amount": num(r.get("achieved_amount")),
                "write_date": pdt(r.get("write_date")),
                "last_seen_at": run_ts,
            }
            heads[row["odoo_id"]] = row
            rows_head.append(row)

    # ---------- líneas ----------
    desired_line = ["id", "budget_analytic_id", "company_id", "currency_id",
                    "date_from", "date_to", "budget_amount", "committed_amount",
                    "achieved_amount", "theoritical_amount",
                    "achieved_percentage", "committed_percentage", "write_date"]
    if tiene_dist:
        desired_line.append("analytic_distribution")
    desired_line += campos_an
    f_line = avail(meta_line, desired_line, "budget.line")

    rows_line: List[dict] = []
    for batch in odoo.search_read_all("budget.line", [], f_line):
        for r in batch:
            bid, bname = m2o(r.get("budget_analytic_id"))
            head = heads.get(bid or -1) or {}
            cid, cname = m2o(r.get("company_id"))
            curid, curname = m2o(r.get("currency_id"))

            # Los campos de plano se guardan CRUDOS. Qué hacer cuando hay más de
            # uno lo decide la vista v_budget_line_analytic, no este script.
            planes: Dict[str, int] = {}
            for f in campos_an:
                aid, _ = m2o(r.get(f))
                if aid:
                    planes[f] = aid

            dist = r.get("analytic_distribution") if tiene_dist else None
            if not isinstance(dist, dict) or not dist:
                dist = None

            rows_line.append({
                "odoo_id": int(r["id"]),
                "budget_id": bid,
                "budget_name": bname or head.get("name"),
                "budget_state": head.get("state"),
                "budget_type": head.get("budget_type"),
                "date_from": pdate(r.get("date_from")) or head.get("date_from"),
                "date_to": pdate(r.get("date_to")) or head.get("date_to"),
                "company_id": cid or head.get("company_id"),
                "company_name": cname or head.get("company_name"),
                "currency_id": curid or head.get("currency_id"),
                "currency_name": curname or head.get("currency_name"),
                "analytic_distribution": {str(k): v for k, v in dist.items()} if dist else None,
                "analytic_plan_fields": planes or None,
                "budget_amount": num(r.get("budget_amount")),
                "committed_amount": num(r.get("committed_amount")),
                "achieved_amount": num(r.get("achieved_amount")),
                "theoritical_amount": num(r.get("theoritical_amount")),
                "achieved_percentage": num(r.get("achieved_percentage")),
                "committed_percentage": num(r.get("committed_percentage")),
                "write_date": pdt(r.get("write_date")),
                "last_seen_at": run_ts,
            })

    con_plan = sum(1 for r in rows_line if r["analytic_plan_fields"])
    con_dist = sum(1 for r in rows_line if r["analytic_distribution"])
    multi = sum(1 for r in rows_line
                if r["analytic_plan_fields"] and len(r["analytic_plan_fields"]) > 1)
    sin_nada = sum(1 for r in rows_line
                   if not r["analytic_plan_fields"] and not r["analytic_distribution"])

    print(f"Cabeceras: {len(rows_head)}   Líneas: {len(rows_line)}")
    print(f"  con campo de plan analítico : {con_plan}")
    print(f"  con analytic_distribution   : {con_dist}")
    print(f"  con MÁS de un plan          : {multi}")
    print(f"  sin ningún vínculo          : {sin_nada}")
    if rows_line:
        print(f"Ejemplo: {rows_line[0]['budget_name'][:60]!r} → "
              f"planes={rows_line[0]['analytic_plan_fields']}")

    if DRY_RUN:
        print("\n--dry-run: no se escribió nada.")
        return 0

    upsert("rpc_upsert_odoo_budgets", rows_head)
    print(f"✅ odoo_budgets: {len(rows_head)} filas")
    upsert("rpc_upsert_odoo_budget_lines", rows_line)
    print(f"✅ odoo_budget_lines: {len(rows_line)} filas")

    try:
        res = sb.rpc("rpc_mark_deleted_odoo_budgets", {"run_ts": run_ts}).execute()
        n = getattr(res, "data", 0) or 0
        if n:
            print(f"🪦 {n} registros marcados como eliminados")
    except Exception as e:
        print(f"⚠️ tombstones: {e}")

    if sin_nada:
        print(f"\n⚠️ {sin_nada} líneas quedaron sin vínculo analítico. "
              f"Revisa: select * from v_budget_lines_sin_analitica;")
    return 0


if __name__ == "__main__":
    sys.exit(main())
