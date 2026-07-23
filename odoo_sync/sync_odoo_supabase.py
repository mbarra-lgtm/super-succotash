from __future__ import annotations

import os
import time
import re
import json
import hashlib
from typing import Any, Dict, List, Optional, Tuple, Iterable
from datetime import datetime, timedelta, timezone, date

import requests
from dateutil import parser as dtp
from supabase import create_client

from zoneinfo import ZoneInfo

# =========================
# ENV
# =========================

STOCK_FULL_RESYNC_HOUR = int(os.getenv("STOCK_FULL_RESYNC_HOUR", "7"))
FULL_RESYNC_SOFT_DELETE_DAYS = int(os.getenv("FULL_RESYNC_SOFT_DELETE_DAYS", "3650"))
PURCHASE_FULL_RESYNC_HOUR = int(os.getenv("PURCHASE_FULL_RESYNC_HOUR", "7"))



TZ_LOCAL = ZoneInfo(os.getenv("TZ_LOCAL", "America/Santiago"))
WORK_START_HOUR = int(os.getenv("WORK_START_HOUR", "7"))   # 07:00
WORK_END_HOUR   = int(os.getenv("WORK_END_HOUR", "19"))    # 19:00 (fin exclusivo)


ODOO_JSONRPC = os.environ["ODOO_JSONRPC"]  # https://bertonati.odoo.com/jsonrpc
ODOO_DB      = os.environ["ODOO_DB"]
ODOO_USER    = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

SUPABASE_URL = os.environ["SUPABASE_URL"]
# Acepta ambos nombres: SUPABASE_SERVICE_ROLE_KEY (este script) o SUPABASE_SERVICE_KEY (pipeline mp_sync)
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_SERVICE_KEY"]

ODOO_LANG = os.getenv("ODOO_LANG", "es_CL")

# Loop period (seconds)
LOOP_EVERY_SECONDS = int(os.getenv("LOOP_EVERY_SECONDS", str(20 * 60)))

# Batch sizes (RPC payloads)
BATCH_PICKINGS   = int(os.getenv("BATCH_PICKINGS", "500"))
BATCH_PBATCHES   = int(os.getenv("BATCH_PBATCHES", "500"))
BATCH_MOVES      = int(os.getenv("BATCH_MOVES", "500"))
BATCH_MOVE_LINES = int(os.getenv("BATCH_MOVE_LINES", "500"))
BATCH_AML        = int(os.getenv("BATCH_AML", "500"))
BATCH_MOVES_HDR  = int(os.getenv("BATCH_MOVES_HDR", "800"))
BATCH_PARTNERS   = int(os.getenv("BATCH_PARTNERS", "800"))
BATCH_TAXES      = int(os.getenv("BATCH_TAXES", "800"))
BATCH_ACCOUNTS   = int(os.getenv("BATCH_ACCOUNTS", "800"))
BATCH_JOURNALS   = int(os.getenv("BATCH_JOURNALS", "800"))
BATCH_PARTIAL_REC = int(os.getenv("BATCH_PARTIAL_REC", "800"))
BATCH_VALUATION = int(os.getenv("BATCH_VALUATION", "1000"))

# Full resync account_moves (1x al día al inicio del horario)
ACCOUNT_MOVES_FULL_RESYNC_HOUR = int(os.getenv("ACCOUNT_MOVES_FULL_RESYNC_HOUR", "7"))
ACCOUNT_MOVES_FULL_RESYNC_FROM = os.getenv("ACCOUNT_MOVES_FULL_RESYNC_FROM", "2024-01-01")

BATCH_AAL = int(os.getenv("BATCH_AAL", "1500"))
BATCH_QUANTS = int(os.getenv("BATCH_QUANTS", "1000"))

# Soft-delete (OFF by default for incremental)
ENABLE_SOFT_DELETE = os.getenv("ENABLE_SOFT_DELETE", "0").strip() == "1"
SOFT_DELETE_DAYS   = int(os.getenv("SOFT_DELETE_DAYS", "5"))

# Control de full-resync para runners efímeros (GitHub Actions), donde el
# centinela por archivo NO sobrevive entre corridas:
#   SKIP_FULL_RESYNC=1  -> nunca corre los full-resync (job frecuente cada 20 min)
#   FORCE_FULL_RESYNC=1 -> corre los full-resync sí o sí (job diario dedicado)
# Con ambos en 0 (default) se mantiene la lógica original por sentinel (uso local en loop).
SKIP_FULL_RESYNC  = os.getenv("SKIP_FULL_RESYNC", "0").strip() == "1"
FORCE_FULL_RESYNC = os.getenv("FORCE_FULL_RESYNC", "0").strip() == "1"

# =========================
# Tables
# =========================
TB_CRM        = "crm_projects"
TB_SALES      = "sales_notes"
TB_MO         = "manufacturing_orders"
TB_BOM        = "mrp_boms"
TB_BOM_LINES  = "mrp_bom_lines"
TB_PICKINGS   = "stock_pickings"
TB_PICKING_BATCHES = "stock_picking_batches"
TB_MOVES      = "stock_moves"
TB_MOVE_LINES = "stock_move_lines"
TB_PRODUCTS   = "product_products"
TB_QUANTS     = "stock_quants"
TB_PURCHASE_ORDERS      = "purchase_orders"
TB_PURCHASE_ORDER_LINES = "purchase_order_lines"
TB_SALE_ORDER_LINES     = "sale_order_lines"
TB_ANALYTIC_ACCOUNTS = "account_analytic_accounts"
TB_PRODUCT_TEMPLATES = "product_templates"

# NEW - Accounting / Tax / Partner
TB_ACCOUNT_MOVES          = "account_moves"
TB_ACCOUNT_MOVE_LINES     = "account_move_lines"
TB_RES_PARTNERS           = "res_partners"
TB_ACCOUNT_TAXES          = "account_taxes"
TB_ACCOUNT_TAX_GROUPS     = "account_tax_groups"
TB_ACCOUNT_ACCOUNTS       = "account_accounts"
TB_ACCOUNT_JOURNALS       = "account_journals"
TB_PARTIAL_RECONCILES     = "account_partial_reconciles"

TB_ACCOUNT_ANALYTIC_LINES = "account_analytic_lines"


#Nuevas tablas

TB_PRODUCT_CATEGORIES      = "product_categories"
TB_STOCK_VALUATION_LAYERS  = "stock_valuation_layers"
TB_ACCOUNT_FULL_RECONCILES = "account_full_reconciles"
TB_RES_COMPANIES           = "res_companies"

TB_CGESTION_TASKS = "cgestion_tasks"
TB_CGESTION_PROJECTS = "cgestion_projects"





# =========================
# Regex MP
# =========================
_RE_MP_ANY = re.compile(r"(\d{3,}-\d{1,3}-[A-Z]{1,4}\d{2})(?:/(\d+))?", re.IGNORECASE)

def parse_mp_from_name(name: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
    if not name:
        return None, None
    m = _RE_MP_ANY.search(name)
    if not m:
        return None, None
    code = m.group(1).upper()
    item = int(m.group(2)) if m.group(2) else None
    return code, item

# =========================
# Parse helpers
# =========================
def parse_odoo_date(value: Any) -> Optional[str]:
    if value is False or value is None or value == "":
        return None
    try:
        dt = dtp.parse(str(value))
        return dt.date().isoformat()
    except Exception:
        return None

def parse_odoo_bool(value: Any) -> Optional[bool]:
    if value is False or value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true","1","t","yes","y","si","sí"):
        return True
    if s in ("false","0","f","no","n"):
        return False
    return None

def parse_odoo_dt(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    dt = dtp.parse(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def m2o(val: Any) -> Tuple[Optional[int], Optional[str]]:
    if not val:
        return None, None
    if isinstance(val, list) and len(val) >= 2:
        return int(val[0]), str(val[1])
    if isinstance(val, list) and len(val) == 1:
        return int(val[0]), None
    if isinstance(val, int):
        return val, None
    return None, str(val)

def chunked(items: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i+size]

def uniq_ints(values: Iterable[Optional[int]]) -> List[int]:
    out: List[int] = []
    seen = set()
    for v in values:
        if v is None:
            continue
        iv = int(v)
        if iv not in seen:
            seen.add(iv)
            out.append(iv)
    return out

# =========================
# Hash helper
# =========================
def make_hash(d: dict, keys: List[str]) -> str:
    payload = {k: d.get(k) for k in keys}
    s = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def ensure_sales_orders_exist(odoo: OdooClient, order_ids: List[int]) -> int:
    """Backfill de sale.order (headers) por ids, para cumplir FK con sale_order_lines."""
    ids = uniq_ints(order_ids)
    if not ids:
        return 0

    desired = ["id", "name", "state", "date_order", "amount_total", "opportunity_id", "partner_id", "write_date"]
    fields = available_fields(odoo, "sale.order", desired)

    total = 0
    for part in chunked(ids, 300):
        domain = [["id", "in", part]]
        batch = odoo.search_read("sale.order", domain, fields, limit=100000, offset=0, order="id asc", context={"active_test": False}) or []
        rows = []
        for r in batch:
            opp_id, _ = m2o(r.get("opportunity_id"))
            partner_id, partner_name = m2o(r.get("partner_id"))
            rows.append({
                "odoo_id": int(r["id"]),
                "name": r.get("name"),
                "state": r.get("state"),
                "date_order": parse_odoo_dt(r.get("date_order")),
                "amount_total": r.get("amount_total"),
                "opportunity_id": opp_id,
                "partner_id": partner_id,
                "partner_name": partner_name,
                "write_date": parse_odoo_dt(r.get("write_date")),
            })
        sb_upsert_basic(TB_SALES, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    return total

def backfill_missing_product_templates(odoo: OdooClient, chunk_ids: int = 300) -> int:
    # trae ids faltantes desde Supabase
    res = sb.table(TB_PRODUCTS).select("odoo_id").is_("product_tmpl_id", "null").limit(20000).execute()
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"Supabase error leyendo {TB_PRODUCTS} null templates: {err}")
    ids = [int(r["odoo_id"]) for r in (getattr(res, "data", None) or [])]
    if not ids:
        print("✅ product_products: no hay product_tmpl_id nulos")
        return 0

    desired = ["id", "product_tmpl_id", "write_date"]
    fields = available_fields(odoo, "product.product", desired)

    rows: List[dict] = []
    for part in chunked(ids, chunk_ids):
        batch = odoo.search_read("product.product", [["id", "in", part]], fields, limit=100000, offset=0, order="id asc", context=CTX_ALL_PRODUCTS) or []
        for r in batch:
            pid = int(r["id"])
            tmpl_id, _ = m2o(r.get("product_tmpl_id"))
            rows.append({
                "odoo_id": pid,
                "product_tmpl_id": tmpl_id,
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

    if rows:
        sb_upsert_basic(TB_PRODUCTS, rows, on_conflict="odoo_id", batch_size=1000)

    print(f"✅ product_products backfill templates: {len(rows)} actualizados")
    return len(rows)

def sync_account_analytic_lines_incremental(odoo: OdooClient, run_ts_iso: str, chunk: int = 1500) -> int:
    """
    Sincroniza account.analytic.line (libro analítico).
    Es la tabla fuente para la clasificación satelital por proyecto × naturaleza.
    
    Tabla pesada → usa RPC con hash igual que account_move_lines.
    """
    model = "account.analytic.line"
    table = TB_ACCOUNT_ANALYTIC_LINES
 
    desired = [
        "id",
        "name",
        "date",
        "amount",
        "unit_amount",
        "account_id",           # cuenta analítica (proyecto)
        "partner_id",
        "product_id",
        "product_uom_id",
        "company_id",
        "currency_id",
        "move_line_id",         # ← clave para cruzar con account_move_lines
        "general_account_id",   # cuenta financiera del asiento relacionado
        "ref",
        "category",             # selection: invoice, other, etc
        "create_date",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)
 
    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])
 
    # Cache de cuentas financieras para enriquecer código y nombre
    acct_map: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    try:
        acct_fields = available_fields(odoo, "account.account", ["id", "code", "name"])
        acct_rows = odoo.search_read(
            "account.account",
            [],
            acct_fields,
            limit=100000,
            offset=0,
            order="id asc",
            context={"active_test": False},
        ) or []
        for a in acct_rows:
            acct_map[int(a["id"])] = ((a.get("code") or None), (a.get("name") or None))
    except Exception:
        acct_map = {}
 
    total_rows = 0
    affected = 0
    ctx = {"active_test": False}
 
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        # Enriquecer producto display
        prod_ids_batch: List[int] = []
        for r in batch:
            pid, _ = m2o(r.get("product_id"))
            if pid:
                prod_ids_batch.append(pid)
        prod_map = fetch_product_map(odoo, prod_ids_batch, chunk_ids=300)
 
        rows: List[dict] = []
        for r in batch:
            aal_id = int(r["id"])
 
            account_id, account_name = m2o(r.get("account_id"))
            partner_id, partner_name = m2o(r.get("partner_id"))
            product_id, _product_name = m2o(r.get("product_id"))
            product_uom_id, product_uom_name = m2o(r.get("product_uom_id"))
            company_id, company_name = m2o(r.get("company_id"))
            currency_id, currency_name = m2o(r.get("currency_id"))
            move_line_id, _ = m2o(r.get("move_line_id"))
            general_account_id, _ = m2o(r.get("general_account_id"))
 
            # Display del producto desde cache
            pinfo = prod_map.get(int(product_id or 0), {"display": None})
            product_display = pinfo.get("display")
 
            # Código y nombre de cuenta financiera desde cache
            financial_account_code = None
            financial_account_name = None
            if general_account_id and int(general_account_id) in acct_map:
                financial_account_code, financial_account_name = acct_map[int(general_account_id)]
 
            row = {
                "odoo_id": aal_id,
                "name": r.get("name"),
                "date": parse_odoo_date(r.get("date")),
 
                "amount": r.get("amount"),
                "unit_amount": r.get("unit_amount"),
 
                # Cuenta analítica (proyecto)
                "account_id": account_id,
                "account_name": account_name,
 
                "partner_id": partner_id,
                "partner_name": partner_name,
 
                "product_id": product_id,
                "product_name": product_display,
                "product_uom_id": product_uom_id,
                "product_uom_name": product_uom_name,
 
                "company_id": company_id,
                "company_name": company_name,
 
                "currency_id": currency_id,
                "currency_name": currency_name,
 
                # Cruce con contabilidad financiera
                "move_line_id": move_line_id,
                "general_account_id": general_account_id,
                "financial_account_code": financial_account_code,
                "financial_account_name": financial_account_name,
 
                "ref": r.get("ref"),
                "category": r.get("category"),
 
                "create_date": parse_odoo_dt(r.get("create_date")),
                "write_date": parse_odoo_dt(r.get("write_date")),
 
                "last_seen_at": run_ts_iso,
                "is_active": True,
                "missing_since": None,
            }
 
            row["row_hash"] = make_hash(row, [
                "odoo_id",
                "name",
                "date",
                "amount",
                "unit_amount",
                "account_id",
                "account_name",
                "partner_id",
                "partner_name",
                "product_id",
                "product_name",
                "product_uom_id",
                "product_uom_name",
                "company_id",
                "company_name",
                "currency_id",
                "currency_name",
                "move_line_id",
                "general_account_id",
                "financial_account_code",
                "financial_account_name",
                "ref",
                "category",
                "create_date",
                "write_date",
                "is_active",
                "missing_since",
            ])
 
            rows.append(row)
 
        total_rows += len(rows)
        affected += sb_rpc_upsert("rpc_upsert_account_analytic_lines", rows, batch_size=BATCH_AAL)
 
    print(f"✅ account_analytic_lines incremental: fetched={total_rows} | db_affected={affected} (desde write_date>{last})")
    return total_rows


def sync_account_moves_full_resync(
    odoo: OdooClient,
    run_ts_iso: str,
    chunk: int = 800,
    date_from: Optional[str] = None,
) -> int:
    """
    Sync FULL de account.move dentro de una ventana de fechas.
 
    Args:
        odoo: cliente RPC
        run_ts_iso: timestamp del inicio del run (marca last_seen_at)
        chunk: tamaño de batch para search_read
        date_from: 'YYYY-MM-DD'. Limita el universo a date >= date_from.
            CRÍTICO: el archivado posterior usa esta misma ventana, así que
            las filas fuera de ella no se tocan.
 
    Returns: filas fetched desde Odoo.
    """
    model = "account.move"
    table = TB_ACCOUNT_MOVES
 
    desired = [
        "id", "name", "ref",
        "move_type", "state",
        "invoice_date", "date", "invoice_date_due",
        "partner_id", "journal_id", "company_id",
        "currency_id",
        "amount_untaxed", "amount_tax", "amount_total",
        "payment_state",
        "invoice_origin",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)
 
    domain: list = []
    if date_from:
        domain.append(["date", ">=", date_from])
 
    total = 0
    ctx = {"active_test": False}
 
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            partner_id, partner_name = m2o(r.get("partner_id"))
            journal_id, journal_name = m2o(r.get("journal_id"))
            company_id, company_name = m2o(r.get("company_id"))
            currency_id, currency_name = m2o(r.get("currency_id"))
 
            rows.append({
                "odoo_id": int(r["id"]),
                "name": r.get("name"),
                "ref": r.get("ref"),
                "move_type": r.get("move_type"),
                "state": r.get("state"),
                "invoice_date": parse_odoo_date(r.get("invoice_date")),
                "date": parse_odoo_date(r.get("date")),
                "invoice_date_due": parse_odoo_date(r.get("invoice_date_due")),
                "partner_id": partner_id,
                "partner_name": partner_name,
                "journal_id": journal_id,
                "journal_name": journal_name,
                "company_id": company_id,
                "company_name": company_name,
                "currency_id": currency_id,
                "currency_name": currency_name,
                "amount_untaxed": r.get("amount_untaxed"),
                "amount_tax": r.get("amount_tax"),
                "amount_total": r.get("amount_total"),
                "payment_state": r.get("payment_state"),
                "invoice_origin": r.get("invoice_origin"),
                "write_date": parse_odoo_dt(r.get("write_date")),
                "last_seen_at": run_ts_iso,    # ← clave para el archivado
            })
 
        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)
 
    print(f"✅ account_moves FULL resync: {total} filas | date_from={date_from}")
    return total

def sync_account_move_lines_full_resync(
    odoo: OdooClient,
    run_ts_iso: str,
    chunk: int = 1500,
    date_from: Optional[str] = None,
) -> int:
    """
    Sync FULL de account.move.line dentro de una ventana de fechas.
 
    No hacemos archivado independiente para AML: cuando archivamos un
    account_moves, sus líneas se archivan en cascada vía la RPC. AML
    sueltas sin header serían un bug del modelo de Odoo, no de aquí.
    """
    model = "account.move.line"
    table = TB_ACCOUNT_MOVE_LINES
 
    desired = [
        "id", "move_id", "name",
        "account_id", "partner_id", "company_id", "journal_id",
        "debit", "credit", "balance",
        "amount_currency", "currency_id",
        "date", "date_maturity",
        "product_id", "quantity", "price_unit",
        "display_type", "analytic_distribution",
        "tax_ids", "tax_line_id",
        "reconciled", "full_reconcile_id",
        "amount_residual", "amount_residual_currency",
        "matched_debit_ids", "matched_credit_ids",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)
 
    domain: list = []
    if date_from:
        domain.append(["date", ">=", date_from])
 
    # Cache cuentas (igual que en el incremental)
    acct_map: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    try:
        acct_fields = available_fields(odoo, "account.account", ["id", "code", "name"])
        acct_rows = odoo.search_read(
            "account.account", [], acct_fields,
            limit=100000, offset=0, order="id asc",
            context={"active_test": False},
        ) or []
        for a in acct_rows:
            acct_map[int(a["id"])] = ((a.get("code") or None), (a.get("name") or None))
    except Exception:
        acct_map = {}
 
    total_rows = 0
    affected = 0
    ctx = {"active_test": False, "check_move_validity": False}
 
    def _coerce_num(v):
        if v is False or v is None:
            return None
        return v
 
    def _coerce_bool(v):
        if v is None or v is False or v is True:
            return v
        return None
 
    def _coerce_id_list(v):
        if v is False or v is None:
            return None
        if isinstance(v, list):
            return [int(x) for x in v if x not in (False, None)]
        return None
 
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            aml_id = int(r["id"])
 
            move_id, _ = m2o(r.get("move_id"))
            account_id, _ = m2o(r.get("account_id"))
            partner_id, _ = m2o(r.get("partner_id"))
            company_id, company_name = m2o(r.get("company_id"))
            journal_id, journal_name = m2o(r.get("journal_id"))
            currency_id, _ = m2o(r.get("currency_id"))
            product_id, _ = m2o(r.get("product_id"))
            tax_line_id, _ = m2o(r.get("tax_line_id"))
            full_reconcile_id, _ = m2o(r.get("full_reconcile_id"))
 
            account_code = None
            account_name = None
            if account_id and int(account_id) in acct_map:
                account_code, account_name = acct_map[int(account_id)]
 
            ad = r.get("analytic_distribution")
            if ad is False:
                ad = None
 
            tax_ids = r.get("tax_ids")
            if tax_ids is False:
                tax_ids = None
            if isinstance(tax_ids, list):
                tax_ids = [int(x) for x in tax_ids if x]
 
            row = {
                "odoo_id": aml_id,
                "move_id": move_id,
                "name": r.get("name") or None,
                "account_id": account_id,
                "account_code": account_code,
                "account_name": account_name,
                "partner_id": partner_id,
                "company_id": company_id,
                "company_name": company_name,
                "journal_id": journal_id,
                "journal_name": journal_name,
                "debit": _coerce_num(r.get("debit")),
                "credit": _coerce_num(r.get("credit")),
                "balance": _coerce_num(r.get("balance")),
                "amount_currency": _coerce_num(r.get("amount_currency")),
                "currency_id": currency_id,
                "date": parse_odoo_date(r.get("date")),
                "date_maturity": parse_odoo_date(r.get("date_maturity")),
                "product_id": product_id,
                "quantity": _coerce_num(r.get("quantity")),
                "price_unit": _coerce_num(r.get("price_unit")),
                "display_type": r.get("display_type") or None,
                "analytic_distribution": ad,
                "tax_line_id": tax_line_id,
                "tax_ids": tax_ids,
                "reconciled": _coerce_bool(r.get("reconciled")),
                "full_reconcile_id": full_reconcile_id,
                "amount_residual": _coerce_num(r.get("amount_residual")),
                "amount_residual_currency": _coerce_num(r.get("amount_residual_currency")),
                "matched_debit_ids": _coerce_id_list(r.get("matched_debit_ids")),
                "matched_credit_ids": _coerce_id_list(r.get("matched_credit_ids")),
                "write_date": parse_odoo_dt(r.get("write_date")),
                "last_seen_at": run_ts_iso,
                "is_active": True,
                "missing_since": None,
            }
 
            row["row_hash"] = make_hash(row, [
                "odoo_id", "move_id", "name",
                "account_id", "account_code", "account_name",
                "partner_id", "company_id", "company_name",
                "journal_id", "journal_name",
                "debit", "credit", "balance",
                "amount_currency", "currency_id",
                "date", "date_maturity",
                "product_id", "quantity", "price_unit",
                "display_type", "analytic_distribution",
                "tax_line_id", "tax_ids",
                "reconciled", "full_reconcile_id",
                "amount_residual", "amount_residual_currency",
                "matched_debit_ids", "matched_credit_ids",
                "write_date", "is_active", "missing_since",
            ])
 
            rows.append(row)
 
        total_rows += len(rows)
        affected += sb_rpc_upsert("rpc_upsert_account_move_lines", rows, batch_size=BATCH_AML)
 
    print(f"✅ account_move_lines FULL resync: fetched={total_rows} | "
          f"db_affected={affected} | date_from={date_from}")
    return total_rows


def archive_orphan_account_moves(run_ts_iso: str, date_from: Optional[str] = None) -> dict:
    """
    Llama a rpc_archive_orphan_account_moves(run_ts, date_from).
    Archiva en account_moves_archive / account_move_lines_archive y luego borra.
 
    IMPORTANTE: solo llamar DESPUÉS de haber corrido los dos FULL resync con
    el mismo run_ts_iso y date_from. Si no, vas a archivar/borrar TODO.
    """
    payload = {"run_ts": run_ts_iso, "date_from": date_from}
    res = sb.rpc("rpc_archive_orphan_account_moves", payload).execute()
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"rpc_archive_orphan_account_moves error: {err}")
 
    data = getattr(res, "data", None) or []
    # PostgREST devuelve una lista de filas; nuestra RPC devuelve 1 fila
    if isinstance(data, list) and data:
        row = data[0]
        archived_lines = row.get("archived_lines", 0)
        archived_moves = row.get("archived_moves", 0)
    elif isinstance(data, dict):
        archived_lines = data.get("archived_lines", 0)
        archived_moves = data.get("archived_moves", 0)
    else:
        archived_lines = 0
        archived_moves = 0
 
    print(f"✅ Archivado account_moves: {archived_moves} headers, {archived_lines} líneas | "
          f"run_ts={run_ts_iso} | date_from={date_from}")
    return {"archived_lines": archived_lines, "archived_moves": archived_moves}



def full_resync_account_moves_with_archive(
    odoo: OdooClient,
    run_ts_iso: str,
    date_from: str = "2024-01-01",
    chunk_moves: int = 800,
    chunk_lines: int = 1500,
) -> dict:
    """
    Orquesta los 3 pasos: fetch headers FULL, fetch lines FULL, archivar+borrar.
 
    El orden importa:
      1) Headers primero (para que last_seen_at esté actualizado antes del
         archivado, que lee de account_moves).
      2) Lines después (por consistencia, aunque la RPC archive las líneas
         por header_id, no por last_seen_at de la línea).
      3) Archivado al final.
    """
    print(f"🔄 FULL resync account_moves desde {date_from} | run_ts={run_ts_iso}")
 
    moves_fetched = sync_account_moves_full_resync(
        odoo, run_ts_iso, chunk=chunk_moves, date_from=date_from
    )
    lines_fetched = sync_account_move_lines_full_resync(
        odoo, run_ts_iso, chunk=chunk_lines, date_from=date_from
    )
    archive_result = archive_orphan_account_moves(run_ts_iso, date_from=date_from)
 
    summary = {
        "moves_fetched": moves_fetched,
        "lines_fetched": lines_fetched,
        **archive_result,
    }
    print(f"📊 Resumen FULL resync: {summary}")
    return summary


# =========================
# Odoo JSON-RPC client
# =========================
class OdooClient:
    def __init__(self, url: str, db: str, user: str, api_key: str):
        self.url = url
        self.db = db
        self.user = user
        self.api_key = api_key
        self.default_context = {"lang": ODOO_LANG}
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})
        self.uid = self.login()

    def _call(self, service: str, method: str, args: list, retries: int = 5) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": 1,
        }
        last_err = None
        for i in range(retries):
            try:
                r = self.s.post(self.url, json=payload, timeout=180)
                r.raise_for_status()
                out = r.json()
                if out.get("error"):
                    raise RuntimeError(out["error"])
                return out.get("result")
            except Exception as e:
                last_err = e
                time.sleep(1.5 * (i + 1))
        raise RuntimeError(f"Odoo JSON-RPC failed: {last_err}")

    def login(self) -> int:
        uid = self._call("common", "login", [self.db, self.user, self.api_key])
        if not uid:
            raise RuntimeError("Login Odoo falló (uid vacío).")
        return int(uid)

    def execute_kw(self, model: str, method: str, args: list, kwargs: Optional[dict] = None) -> Any:
        kwargs = kwargs or {}
        ctx = dict(self.default_context)
        if kwargs.get("context"):
            ctx.update(kwargs["context"])
        kwargs["context"] = ctx
        return self._call("object", "execute_kw", [self.db, self.uid, self.api_key, model, method, args, kwargs])

    def search_read(
        self,
        model: str,
        domain: list,
        fields: list,
        limit: int,
        offset: int,
        order: str = "id asc",
        context: Optional[dict] = None,
    ):
        kwargs = {"fields": fields, "limit": limit, "offset": offset, "order": order}
        if context is not None:
            kwargs["context"] = context
        return self.execute_kw(model, "search_read", [domain], kwargs)

    def fields_get(self, model: str) -> Dict[str, Any]:
        return self.execute_kw(model, "fields_get", [], {"attributes": ["string", "type"]})

def iter_search_read_all(
    odoo: OdooClient,
    model: str,
    domain: list,
    fields: list,
    chunk: int,
    context: Optional[dict] = None,
) -> Iterable[List[Dict[str, Any]]]:
    offset = 0
    while True:
        batch = odoo.search_read(model, domain, fields, limit=chunk, offset=offset, order="id asc", context=context)
        if not batch:
            break
        yield batch
        offset += len(batch)

def available_fields(odoo: OdooClient, model: str, desired: List[str]) -> List[str]:
    meta = odoo.fields_get(model)
    have = set(meta.keys())
    ok = [f for f in desired if f in have]
    missing = [f for f in desired if f not in have]
    if missing:
        print(f"⚠️ {model}: campos no disponibles y se omiten: {missing}")
    return ok

# =========================
# Supabase client + helpers
# =========================
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

def sb_upsert_basic(table: str, rows: List[dict], on_conflict: str = "odoo_id", batch_size: int = 1000) -> None:
    """Para tablas NO pesadas (CRM, Sales, MO, BOM, Products, Quants)"""
    if not rows:
        return
    for part in chunked(rows, batch_size):
        res = sb.table(table).upsert(part, on_conflict=on_conflict).execute()
        err = getattr(res, "error", None)
        if err:
            raise RuntimeError(f"Supabase error upsert({table}): {err}")

def sb_rpc_upsert(func_name: str, rows: List[dict], batch_size: int) -> int:
    """Para tablas PESADAS, usando RPC con hash + update condicional"""
    if not rows:
        return 0
    total = 0
    for part in chunked(rows, batch_size):
        res = sb.rpc(func_name, {"payload": part}).execute()
        err = getattr(res, "error", None)
        if err:
            raise RuntimeError(f"Supabase RPC error {func_name}: {err}")
        # res.data suele ser el retorno de la función (bigint)
        val = 0
        data = getattr(res, "data", None)
        if isinstance(data, (int, float)):
            val = int(data)
        elif isinstance(data, list) and data and isinstance(data[0], (int, float)):
            val = int(data[0])
        total += val
    return total

def sb_get_max_write_date(table: str) -> Optional[str]:
    res = sb.table(table).select("write_date").order("write_date", desc=True).limit(1).execute()
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"Supabase error leyendo max write_date de {table}: {err}")
    data = getattr(res, "data", None) or []
    if not data:
        return None
    return data[0].get("write_date")


# =========================
# CGestion
# =========================

def sync_cgestion_tasks_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "x_cgestion"
    table = TB_CGESTION_TASKS

    desired = [
        "id",
        "x_name",                           # Actividad (título)
        "x_studio_stage_id",                # Etapa (many2one)
        "x_studio_kanban_state",            # Estado de kanban (selection)
        "x_studio_clasificacin",            # Clasificación (selection)
        "x_studio_prioridad",               # Prioridad (integer, editable)
        "x_studio_indice_de_priorizacin",   # Índice de Priorización (integer, readonly)
        "x_studio_importancia",             # Importancia (integer)
        "x_studio_urgencia",                # Urgencia (integer)
        "x_studio_priority",                # Alta prioridad (boolean)
        "x_studio_rea_del_proyecto",        # Macro Proyecto (selection)
        "x_studio_proyecto_segundo_nivel",  # Sub Proyecto (selection)
        "x_studio_rea",                     # Área (selection)
        "x_studio_empresa",                 # Empresa (selection)
        "x_studio_proyecto_tdp",            # Proyecto TDP (many2one) ← clave
        "x_studio_proyecto",                # Proyecto (many2one)
        "x_studio_subproyecto",             # Subproyecto (many2one)
        "x_studio_sistema",                 # Sistema (selection)
        "x_studio_usuario_responsable",     # Usuario Responsable (many2one)
        "x_studio_analista_responsable",    # Analista Responsable (many2one)
        "x_studio_revisado_por",            # Asignado a (many2one)
        "x_studio_revisado_por_1",          # Revisado por (many2one)
        "x_studio_user_id",                 # Responsable (many2one)
        "x_studio_tarea",                   # Detalle (text)
        "x_studio_fecha_planificada_de_inicio", # Fecha Planificada de Inicio (date)
        "x_studio_date",                    # Fecha de Término (date)
        "x_studio_date_start",              # Rango Duración Estimado inicio (datetime)
        "x_studio_date_stop",               # Fecha de finalización (datetime)
        "x_studio_fecha_asignada",          # Fecha Asignada (date)
        "x_studio_solicitar_revisin",       # Solicitar Revisión (boolean)
        "x_studio_calidad",                 # Calidad (boolean)
        "x_studio_comercial",               # Comercial (boolean)
        "x_studio_compras",                 # Compras (boolean)
        "x_studio_operaciones",             # Operaciones (boolean)
        "x_studio_bodega",                  # Bodega (boolean)
        "x_studio_contabilidad",            # Contabilidad (boolean)
        "x_studio_personas_ssgg",           # Personas / SSGG (boolean)
        "x_active",                         # Activo (boolean)
        "create_date",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    ctx = {"active_test": False}

    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            stage_id, stage_name             = m2o(r.get("x_studio_stage_id"))
            proyecto_tdp_id, proyecto_tdp    = m2o(r.get("x_studio_proyecto_tdp"))
            proyecto_id, proyecto_nombre     = m2o(r.get("x_studio_proyecto"))
            subproyecto_id, subproyecto      = m2o(r.get("x_studio_subproyecto"))
            responsable_id, responsable      = m2o(r.get("x_studio_usuario_responsable"))
            analista_id, analista            = m2o(r.get("x_studio_analista_responsable"))
            asignado_id, asignado            = m2o(r.get("x_studio_revisado_por"))
            revisado_id, revisado_por        = m2o(r.get("x_studio_revisado_por_1"))
            user_id, user_name               = m2o(r.get("x_studio_user_id"))

            rows.append({
                "odoo_id":                   int(r["id"]),
                "nombre":                    (r.get("x_name") or "").strip() or None,
                "activo":                    r.get("x_active"),

                # Etapa / estado
                "stage_id":                  stage_id,
                "stage_nombre":              stage_name,
                "kanban_state":              r.get("x_studio_kanban_state"),
                "clasificacion":             r.get("x_studio_clasificacin"),

                # Priorización
                "prioridad":                 r.get("x_studio_prioridad"),
                "prioridad_alta":            r.get("x_studio_priority"),
                "importancia":               r.get("x_studio_importancia"),
                "urgencia":                  r.get("x_studio_urgencia"),
                "indice_priorizacion":       r.get("x_studio_indice_de_priorizacin"),

                # Jerarquía proyecto
                "macro_proyecto":            r.get("x_studio_rea_del_proyecto"),
                "sub_proyecto_sel":          r.get("x_studio_proyecto_segundo_nivel"),
                "area":                      r.get("x_studio_rea"),
                "empresa":                   r.get("x_studio_empresa"),
                "proyecto_tdp_id":           proyecto_tdp_id,
                "proyecto_tdp_nombre":       proyecto_tdp,
                "proyecto_id":               proyecto_id,
                "proyecto_nombre":           proyecto_nombre,
                "subproyecto_id":            subproyecto_id,
                "subproyecto_nombre":        subproyecto,
                "sistema":                   r.get("x_studio_sistema"),

                # Personas
                "responsable_id":            responsable_id,
                "responsable":               responsable,
                "analista_id":               analista_id,
                "analista":                  analista,
                "asignado_id":               asignado_id,
                "asignado":                  asignado,
                "revisado_por_id":           revisado_id,
                "revisado_por":              revisado_por,
                "user_id":                   user_id,
                "user_nombre":               user_name,

                # Contenido
                "detalle":                   r.get("x_studio_tarea"),
                "solicitar_revision":        r.get("x_studio_solicitar_revisin"),

                # Áreas relacionadas (booleans)
                "area_calidad":              r.get("x_studio_calidad"),
                "area_comercial":            r.get("x_studio_comercial"),
                "area_compras":              r.get("x_studio_compras"),
                "area_operaciones":          r.get("x_studio_operaciones"),
                "area_bodega":               r.get("x_studio_bodega"),
                "area_contabilidad":         r.get("x_studio_contabilidad"),
                "area_personas_ssgg":        r.get("x_studio_personas_ssgg"),

                # Fechas
                "fecha_planificada_inicio":  parse_odoo_date(r.get("x_studio_fecha_planificada_de_inicio")),
                "fecha_termino":             parse_odoo_date(r.get("x_studio_date")),
                "fecha_asignada":            parse_odoo_date(r.get("x_studio_fecha_asignada")),
                "fecha_inicio_estimado":     parse_odoo_dt(r.get("x_studio_date_start")),
                "fecha_fin_estimado":        parse_odoo_dt(r.get("x_studio_date_stop")),

                "create_date":               parse_odoo_dt(r.get("create_date")),
                "write_date":                parse_odoo_dt(r.get("write_date")),
            })

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ cgestion_tasks incremental: {total} filas (desde write_date>{last})")
    return total    


def sync_cgestion_projects_incremental(odoo: OdooClient, chunk: int = 500) -> int:
    model = "x_tdigital_project"
    table = TB_CGESTION_PROJECTS
 
    desired = [
        "id",
        "x_name",                           # Descripción (nombre interno)
        "x_studio_nombre_del_proyecto",     # Nombre del Proyecto
        "x_studio_codigo_del_proyecto",     # Código del Proyecto
        "x_active",                         # Activo
        "x_studio_stage_id",                # Etapa (many2one)
        "x_studio_kanban_state",            # Estado de kanban (selection)
        "x_studio_prioridad",               # Prioridad (selection: P1 Alta / P2 Media / P3 Baja)
        "x_studio_tipo_de_proyecto",        # Tipo de Proyecto (selection)
        "x_studio_tipologia_del_proyecto",  # Tipología del Proyecto (selection)
        "x_studio_responsable",             # Responsable (many2one)
        "x_studio_responsable_tdp",         # Responsable TDP (selection)
        "x_studio_solicitante",             # Solicitante (char)
        "x_studio_descripcin",              # Descripción
        "x_studio_resultado_esperado",      # Resultado Esperado
        "x_studio_fecha_inicio_planificada",# Fecha Inicio Planificada (date)
        "x_studio_fecha_fin_planificada",   # Fecha Fin Planificada (date)
        "x_studio_fecha_fin_real",          # Fecha Fin Real (date)
        "x_studio_date_start",              # Fecha de inicio (datetime)
        "x_studio_date_stop",               # Fecha de finalización (datetime)
        "x_studio_porcentaje_de_avance",    # % de Avance (float)
        "x_studio_priority",                # Alta prioridad (boolean)
        # Áreas relacionadas
        "x_studio_compras",
        "x_studio_bodega",
        "x_studio_comercial",
        "x_studio_operaciones",
        "x_studio_calidad",
        "x_studio_contabilidad",
        "x_studio_bservice",
        "x_studio_personas_rrhh",
        "create_date",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)
 
    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])
 
    total = 0
    ctx = {"active_test": False}
 
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            stage_id, stage_nombre     = m2o(r.get("x_studio_stage_id"))
            etapa_id, etapa_nombre     = m2o(r.get("x_studio_etapa")) if "x_studio_etapa" in r else (None, None)
            responsable_id, responsable = m2o(r.get("x_studio_responsable"))
 
            def cv(v: Any) -> Optional[str]:
                if v is False or v is None or v == "": return None
                s = str(v).strip()
                return None if s in ("false", "null") else s
 
            rows.append({
                "odoo_id":                      int(r["id"]),
                "nombre":                       cv(r.get("x_studio_nombre_del_proyecto")) or cv(r.get("x_name")),
                "codigo":                       cv(r.get("x_studio_codigo_del_proyecto")),
                "activo":                       r.get("x_active"),
 
                # Etapa / estado
                "stage_id":                     stage_id,
                "stage_nombre":                 cv(stage_nombre),
                "kanban_state":                 cv(r.get("x_studio_kanban_state")),
 
                # Prioridad
                "prioridad":                    cv(r.get("x_studio_prioridad")),       # P1 Alta / P2 Media / P3 Baja
                "prioridad_alta":               r.get("x_studio_priority"),
                "tipo_proyecto":                cv(r.get("x_studio_tipo_de_proyecto")),
                "tipologia":                    cv(r.get("x_studio_tipologia_del_proyecto")),
 
                # Personas
                "responsable_id":               responsable_id,
                "responsable":                  cv(responsable),
                "responsable_tdp":              cv(r.get("x_studio_responsable_tdp")),
                "solicitante":                  cv(r.get("x_studio_solicitante")),
 
                # Descripción
                "descripcion":                  cv(r.get("x_studio_descripcin")),
                "resultado_esperado":           cv(r.get("x_studio_resultado_esperado")),
 
                # Fechas
                "fecha_inicio_planificada":     parse_odoo_date(r.get("x_studio_fecha_inicio_planificada")),
                "fecha_fin_planificada":        parse_odoo_date(r.get("x_studio_fecha_fin_planificada")),
                "fecha_fin_real":               parse_odoo_date(r.get("x_studio_fecha_fin_real")),
                "fecha_inicio_dt":              parse_odoo_dt(r.get("x_studio_date_start")),
                "fecha_fin_dt":                 parse_odoo_dt(r.get("x_studio_date_stop")),
 
                # Avance
                "pct_avance":                   r.get("x_studio_porcentaje_de_avance"),
 
                # Áreas
                "area_compras":                 r.get("x_studio_compras"),
                "area_bodega":                  r.get("x_studio_bodega"),
                "area_comercial":               r.get("x_studio_comercial"),
                "area_operaciones":             r.get("x_studio_operaciones"),
                "area_calidad":                 r.get("x_studio_calidad"),
                "area_contabilidad":            r.get("x_studio_contabilidad"),
                "area_bservice":                r.get("x_studio_bservice"),
                "area_personas_rrhh":           r.get("x_studio_personas_rrhh"),
 
                "create_date":                  parse_odoo_dt(r.get("create_date")),
                "write_date":                   parse_odoo_dt(r.get("write_date")),
            })
 
        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=500)
        total += len(rows)
 
    print(f"✅ cgestion_projects incremental: {total} filas (desde write_date>{last})")
    return total


# =========================
# Products Category
# =========================

def sync_product_categories_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "product.category"
    table = TB_PRODUCT_CATEGORIES

    desired = [
        "id",
        "name",
        "parent_id",
        "complete_name",
        "property_stock_valuation_account_id",
        "property_stock_account_input_categ_id",
        "property_stock_account_output_categ_id",
        "property_account_expense_categ_id",
        "property_account_income_categ_id",
        "property_account_creditor_price_difference_categ",
        "property_stock_account_production_cost_id",   # ← NUEVO
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    ctx = {"active_test": False}

    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []

        for r in batch:
            parent_id, parent_name = m2o(r.get("parent_id"))

            stock_valuation_account_id, stock_valuation_account_name = m2o(r.get("property_stock_valuation_account_id"))
            stock_input_account_id, stock_input_account_name = m2o(r.get("property_stock_account_input_categ_id"))
            stock_output_account_id, stock_output_account_name = m2o(r.get("property_stock_account_output_categ_id"))
            expense_account_id, expense_account_name = m2o(r.get("property_account_expense_categ_id"))
            income_account_id, income_account_name = m2o(r.get("property_account_income_categ_id"))
            price_diff_account_id, price_diff_account_name = m2o(r.get("property_account_creditor_price_difference_categ"))
            production_account_id, production_account_name = m2o(
                r.get("property_stock_account_production_cost_id")
            )
            rows.append({
                "odoo_id": int(r["id"]),
                "name": (r.get("name") or "").strip() or None,
                "parent_id": parent_id,
                "parent_name": parent_name,
                "complete_name": (r.get("complete_name") or "").strip() or None,

                "property_stock_valuation_account_id": stock_valuation_account_id,
                "property_stock_valuation_account_name": stock_valuation_account_name,

                "property_stock_account_input_categ_id": stock_input_account_id,
                "property_stock_account_input_categ_name": stock_input_account_name,

                "property_stock_account_output_categ_id": stock_output_account_id,
                "property_stock_account_output_categ_name": stock_output_account_name,

                "property_account_expense_categ_id": expense_account_id,
                "property_account_expense_categ_name": expense_account_name,

                "property_account_income_categ_id": income_account_id,
                "property_account_income_categ_name": income_account_name,

                "property_account_creditor_price_difference_categ_id": price_diff_account_id,
                "property_account_creditor_price_difference_categ_name": price_diff_account_name,

                # ← AGREGAR ESTAS DOS LÍNEAS:
                "property_stock_account_production_categ_id": production_account_id,
                "property_stock_account_production_categ_name": production_account_name,

                "write_date": parse_odoo_dt(r.get("write_date")),
            })

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ product_categories incremental: {total} filas (desde write_date>{last})")
    return total


# =========================
# Res Company
# =========================

def sync_res_companies_incremental(odoo: OdooClient, chunk: int = 300) -> int:
    model = "res.company"
    table = TB_RES_COMPANIES

    desired = ["id", "name", "partner_id", "currency_id", "write_date"]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    ctx = {"active_test": False}

    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            partner_id, partner_name = m2o(r.get("partner_id"))
            currency_id, currency_name = m2o(r.get("currency_id"))

            rows.append({
                "odoo_id": int(r["id"]),
                "name": (r.get("name") or "").strip() or None,
                "partner_id": partner_id,
                "partner_name": partner_name,
                "currency_id": currency_id,
                "currency_name": currency_name,
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ res_companies incremental: {total} filas (desde write_date>{last})")
    return total


# =========================
# Conciliacion
# =========================


def sync_account_full_reconciles_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "account.full.reconcile"
    table = TB_ACCOUNT_FULL_RECONCILES

    desired = ["id", "name", "create_date", "write_date"]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    ctx = {"active_test": False}

    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            rows.append({
                "odoo_id": int(r["id"]),
                "name": (r.get("name") or "").strip() or None,
                "create_date": parse_odoo_dt(r.get("create_date")),
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ account_full_reconciles incremental: {total} filas (desde write_date>{last})")
    return total


# =========================
# Stock Valuation
# =========================

def sync_stock_valuation_layers_incremental(odoo: OdooClient, run_ts_iso: str, chunk: int = 1200) -> int:
    model = "stock.valuation.layer"
    table = TB_STOCK_VALUATION_LAYERS

    desired = [
        "id",
        "product_id",
        "stock_move_id",
        "company_id",
        "description",
        "quantity",
        "unit_cost",
        "value",
        "remaining_qty",
        "remaining_value",
        "create_date",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total_rows = 0
    affected = 0
    ctx = {"active_test": False}

    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        prod_ids_batch: List[int] = []
        for r in batch:
            pid, _ = m2o(r.get("product_id"))
            if pid:
                prod_ids_batch.append(pid)

        prod_map = fetch_product_map(odoo, prod_ids_batch, chunk_ids=300)

        rows: List[dict] = []
        for r in batch:
            svl_id = int(r["id"])

            product_id, _ = m2o(r.get("product_id"))
            stock_move_id, stock_move_name = m2o(r.get("stock_move_id"))
            company_id, company_name = m2o(r.get("company_id"))

            pinfo = prod_map.get(int(product_id or 0), {})
            product_display = pinfo.get("display") or "—"
            product_tmpl_id = pinfo.get("product_tmpl_id")

            row = {
                "odoo_id": svl_id,
                "product_id": product_id,
                "product_name": product_display,
                "product_tmpl_id": product_tmpl_id,

                "stock_move_id": stock_move_id,
                "stock_move_name": stock_move_name,

                "company_id": company_id,
                "company_name": company_name,

                "description": r.get("description"),
                "quantity": r.get("quantity"),
                "unit_cost": r.get("unit_cost"),
                "value": r.get("value"),
                "remaining_qty": r.get("remaining_qty"),
                "remaining_value": r.get("remaining_value"),

                "create_date": parse_odoo_dt(r.get("create_date")),
                "write_date": parse_odoo_dt(r.get("write_date")),

                "last_seen_at": run_ts_iso,
                "is_active": True,
                "missing_since": None,
            }

            row["row_hash"] = make_hash(row, [
                "odoo_id",
                "product_id", "product_name", "product_tmpl_id",
                "stock_move_id", "stock_move_name",
                "company_id", "company_name",
                "description",
                "quantity", "unit_cost", "value",
                "remaining_qty", "remaining_value",
                "create_date", "write_date",
                "is_active", "missing_since"
            ])

            rows.append(row)

        total_rows += len(rows)
        affected += sb_rpc_upsert("rpc_upsert_stock_valuation_layers", rows, batch_size=BATCH_VALUATION)

    print(f"✅ stock_valuation_layers incremental: fetched={total_rows} | db_affected={affected} (desde write_date>{last})")
    return total_rows

# =========================
# Stock Quant sync
# =========================

def get_location_id_by_complete_name(odoo: OdooClient, complete_name: str) -> Optional[int]:
    rows = odoo.search_read(
        "stock.location",
        [["complete_name", "=", complete_name]],
        ["id", "complete_name"],
        limit=10,
        offset=0,
        order="id asc",
        context={"active_test": False},
    ) or []
    if not rows:
        return None
    return int(rows[0]["id"])

STOCK_QUANT_LOCATION_IDS = [
    int(x) for x in os.getenv(
        "STOCK_QUANT_LOCATION_IDS",
        "8,17,105"
    ).split(",")
]
 
 
def sync_stock_quants_incremental(
    odoo: OdooClient,
    run_ts_iso: str,
    chunk: int = 1500,
    location_ids: Optional[List[int]] = None,
    enable_soft_delete: bool = True,
    soft_delete_days: int = 7,
) -> int:
    """
    Sincroniza stock.quant para las ubicaciones configuradas con
    housekeeping completo (last_seen_at, is_active, missing_since, row_hash).
 
    Args:
        odoo: cliente RPC de Odoo
        run_ts_iso: timestamp del inicio del run (para marcar last_seen_at)
        chunk: tamaño del batch para search_read
        location_ids: lista de location_id a sincronizar. Si None, usa
            STOCK_QUANT_LOCATION_IDS del entorno (default [8, 17, 105]).
        enable_soft_delete: si True, marca como inactivos los quants que
            no se vieron en este run. Default True.
        soft_delete_days: días de gracia antes de eliminar físicamente
            los quants marcados como inactivos. Default 7.
 
    Returns:
        Cantidad de filas fetched desde Odoo (no db_affected).
    """
    model = "stock.quant"
    table = TB_QUANTS
 
    desired = [
        "id", "product_id", "location_id",
        "quantity", "reserved_quantity",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)
 
    # Resolver location_ids — si el usuario no pasó explícito, usar env
    locs = location_ids if location_ids is not None else STOCK_QUANT_LOCATION_IDS
    if not locs:
        print("⚠️ stock_quants: sin location_ids configuradas. Saliendo.")
        return 0
 
    # IMPORTANTE: usamos child_of por cada ubicación para capturar
    # sub-ubicaciones (bins) que cuelgan de cada una.
    # Odoo permite OR entre subdominios con el operador "|".
    # Para 3 ubicaciones: ["|", "|", ["location_id","child_of",8], ["location_id","child_of",17], ["location_id","child_of",105]]
    domain: list = []
    if len(locs) == 1:
        domain.append(["location_id", "child_of", locs[0]])
    else:
        # Agregar (n-1) operadores "|" al inicio, luego los predicados
        for _ in range(len(locs) - 1):
            domain.append("|")
        for lid in locs:
            domain.append(["location_id", "child_of", lid])
 
    # Filtro incremental por write_date (como antes)
    last = sb_get_max_write_date(table)
    if last:
        # Anidar con el filtro de ubicación ya construido
        # Odoo: combinar con "&" no es necesario porque es el default,
        # pero dejamos explícito para evitar confusiones con "|".
        # Truco: insertamos "&" al inicio para forzar AND con lo que sigue
        if domain:
            domain = ["&"] + [domain] if False else domain  # no-op, dejamos claro
        domain.append(["write_date", ">", last])
 
    total_rows = 0
    affected = 0
 
    for batch in iter_search_read_all(
        odoo, model, domain, fields, chunk=chunk,
        context={"active_test": False}
    ):
        rows: List[dict] = []
        for r in batch:
            qid = int(r["id"])
            prod_id, prod_disp = m2o(r.get("product_id"))
            loc_id, loc_disp = m2o(r.get("location_id"))
 
            row = {
                "odoo_id": qid,
                "product_id": prod_id,
                "product_display": prod_disp,
                "location_id": loc_id,
                "location_display": loc_disp,
                "bin_rack": None,  # reservado para futura expansión
                "quantity": r.get("quantity"),
                "reserved_quantity": r.get("reserved_quantity"),
                "write_date": parse_odoo_dt(r.get("write_date")),
                "last_seen_at": run_ts_iso,
                "is_active": True,
                "missing_since": None,
            }
 
            # Hash consistente con los otros syncs pesados
            row["row_hash"] = make_hash(row, [
                "odoo_id",
                "product_id", "product_display",
                "location_id", "location_display",
                "bin_rack",
                "quantity", "reserved_quantity",
                "write_date",
                "is_active", "missing_since",
            ])
 
            rows.append(row)
 
        total_rows += len(rows)
        affected += sb_rpc_upsert(
            "rpc_upsert_stock_quants",
            rows,
            batch_size=BATCH_QUANTS,
        )
 
    print(
        f"✅ stock_quants incremental: fetched={total_rows} | "
        f"db_affected={affected} | locs={locs} "
        f"(desde write_date>{last})"
    )
 
    # ─────────────────────────────────────────────────────────────────
    # Soft-delete de quants que no se vieron en este run.
    # IMPORTANTE: esto sólo es correcto cuando el fetch trae el universo
    # completo de quants relevantes. Como filtramos por ubicaciones Y por
    # write_date > last, NO podemos soft-deletear directamente — un quant
    # que no cambió desde el último sync no aparecería en este batch.
    #
    # Solución: el soft-delete se ejecuta SÓLO cuando last=NULL (primer run)
    # o en modo "full resync" (sin filtro de write_date).
    # Para prod, el soft-delete debe correrse periódicamente (ej. 1 vez
    # al día) con un sync completo — ver sync_stock_quants_full_resync.
    # ─────────────────────────────────────────────────────────────────
 
    if enable_soft_delete and not last:
        try:
            res = sb.rpc(
                "rpc_mark_missing_stock_quants",
                {"run_ts": run_ts_iso, "days": soft_delete_days},
            ).execute()
            err = getattr(res, "error", None)
            if err:
                print(f"⚠️ soft-delete stock_quants: {err}")
            else:
                marked = getattr(res, "data", None) or 0
                print(f"✅ soft-delete stock_quants: {marked} marcados inactivos")
        except Exception as e:
            print(f"⚠️ soft-delete stock_quants: {e}")
 
    return total_rows
 
 
def sync_stock_quants_full_resync(
    odoo: OdooClient,
    run_ts_iso: str,
    chunk: int = 1500,
    location_ids: Optional[List[int]] = None,
    soft_delete_days: int = 7,
) -> int:
    """
    Sync FULL (sin filtro write_date) + soft-delete al final.
 
    Pensado para correr 1 vez al día (ej. al inicio del horario laboral)
    para capturar quants eliminados en Odoo que no se ven vía incremental.
 
    El sync incremental normal (cada 20 min) NO hace soft-delete para no
    invalidar quants que simplemente no cambiaron desde el último run.
    """
    model = "stock.quant"
    table = TB_QUANTS
 
    desired = [
        "id", "product_id", "location_id",
        "quantity", "reserved_quantity",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)
 
    locs = location_ids if location_ids is not None else STOCK_QUANT_LOCATION_IDS
    if not locs:
        print("⚠️ stock_quants full resync: sin location_ids. Saliendo.")
        return 0
 
    # Dominio: sólo filtro de ubicación, sin write_date
    domain: list = []
    if len(locs) == 1:
        domain.append(["location_id", "child_of", locs[0]])
    else:
        for _ in range(len(locs) - 1):
            domain.append("|")
        for lid in locs:
            domain.append(["location_id", "child_of", lid])
 
    total_rows = 0
    affected = 0
 
    for batch in iter_search_read_all(
        odoo, model, domain, fields, chunk=chunk,
        context={"active_test": False}
    ):
        rows: List[dict] = []
        for r in batch:
            qid = int(r["id"])
            prod_id, prod_disp = m2o(r.get("product_id"))
            loc_id, loc_disp = m2o(r.get("location_id"))
 
            row = {
                "odoo_id": qid,
                "product_id": prod_id,
                "product_display": prod_disp,
                "location_id": loc_id,
                "location_display": loc_disp,
                "bin_rack": None,
                "quantity": r.get("quantity"),
                "reserved_quantity": r.get("reserved_quantity"),
                "write_date": parse_odoo_dt(r.get("write_date")),
                "last_seen_at": run_ts_iso,
                "is_active": True,
                "missing_since": None,
            }
            row["row_hash"] = make_hash(row, [
                "odoo_id", "product_id", "product_display",
                "location_id", "location_display",
                "bin_rack", "quantity", "reserved_quantity",
                "write_date", "is_active", "missing_since",
            ])
            rows.append(row)
 
        total_rows += len(rows)
        affected += sb_rpc_upsert("rpc_upsert_stock_quants", rows, batch_size=BATCH_QUANTS)
 
    print(
        f"✅ stock_quants FULL resync: fetched={total_rows} | "
        f"db_affected={affected} | locs={locs}"
    )
 
    # Soft-delete: ahora sí es seguro porque fetchamos el universo completo
    try:
        res = sb.rpc(
            "rpc_mark_missing_stock_quants",
            {"run_ts": run_ts_iso, "days": soft_delete_days},
        ).execute()
        err = getattr(res, "error", None)
        if err:
            print(f"⚠️ soft-delete stock_quants: {err}")
        else:
            marked = getattr(res, "data", None) or 0
            print(f"✅ soft-delete stock_quants: {marked} marcados inactivos")
    except Exception as e:
        print(f"⚠️ soft-delete stock_quants: {e}")
 
    return total_rows
 
 

# =========================
# Product sync + cache
# =========================
_PRODUCT_CACHE: Dict[int, Dict[str, Optional[Any]]] = {}
CTX_ALL_PRODUCTS = {"active_test": False}

def sync_product_products_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "product.product"
    table = TB_PRODUCTS

    desired = ["id", "default_code", "name", "barcode", "active", "write_date", "standard_price", "product_tmpl_id"]
    fields = available_fields(odoo, model, desired)

    domain: list = []
    last = sb_get_max_write_date(table)
    if last:
        domain = [["write_date", ">", last]]

    total = 0
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=CTX_ALL_PRODUCTS):
        rows: List[dict] = []
        for r in batch:
            pid = int(r["id"])
            code = (r.get("default_code") or "").strip() or None
            name = (r.get("name") or "").strip() or None
            barcode = (r.get("barcode") or "").strip() or None
            std = r.get("standard_price")
            tmpl_id, _tmpl_name = m2o(r.get("product_tmpl_id"))


            display = f"[{code}] {name}" if code and name else (name or (code and f"[{code}]") or "—")

            rows.append({
                "odoo_id": pid,
                "default_code": code,
                "name": name,
                "barcode": barcode,
                "standard_price": std,
                "active": r.get("active"),
                "write_date": parse_odoo_dt(r.get("write_date")),
                "product_tmpl_id": tmpl_id,   # ✅ clave

            })

            _PRODUCT_CACHE[pid] = {
                "default_code": code,
                "name": name,
                "display": display,
                "standard_price": std,
                "product_tmpl_id": tmpl_id,
            }


        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ product_products incremental: {total} filas")
    return total

def fetch_product_map(odoo: OdooClient, product_ids: List[int], chunk_ids: int = 300) -> Dict[int, Dict[str, Optional[Any]]]:
    product_ids_u = uniq_ints(product_ids)
    if not product_ids_u:
        return {}

    missing = [pid for pid in product_ids_u if pid not in _PRODUCT_CACHE]
    if missing:
        desired = ["id", "default_code", "name", "barcode", "active", "write_date", "standard_price", "product_tmpl_id"]
        fields = available_fields(odoo, "product.product", desired)

        upsert_rows: List[dict] = []
        for part in chunked(missing, chunk_ids):
            domain = [["id", "in", part]]
            rows = odoo.search_read("product.product", domain, fields, limit=100000, offset=0, order="id asc", context=CTX_ALL_PRODUCTS)
            for r in rows:
                pid = int(r["id"])
                code = (r.get("default_code") or "").strip() or None
                name = (r.get("name") or "").strip() or None
                barcode = (r.get("barcode") or "").strip() or None
                std = r.get("standard_price")

                tmpl_id, _ = m2o(r.get("product_tmpl_id"))  # ✅ AQUÍ faltaba

                display = f"[{code}] {name}" if code and name else (name or (code and f"[{code}]") or "—")

                _PRODUCT_CACHE[pid] = {
                    "default_code": code,
                    "name": name,
                    "display": display,
                    "standard_price": std,
                    "product_tmpl_id": tmpl_id,  # ✅ ok
                }

                upsert_rows.append({
                    "odoo_id": pid,
                    "default_code": code,
                    "name": name,
                    "barcode": barcode,
                    "standard_price": std,
                    "active": r.get("active"),
                    "write_date": parse_odoo_dt(r.get("write_date")),
                    "product_tmpl_id": tmpl_id,  # ✅ ok
                })


        if upsert_rows:
            sb_upsert_basic(TB_PRODUCTS, upsert_rows, on_conflict="odoo_id", batch_size=1000)

        for pid in missing:
            if pid not in _PRODUCT_CACHE:
                _PRODUCT_CACHE[pid] = {"default_code": None, "name": None, "display": "—", "standard_price": None}

    return {pid: _PRODUCT_CACHE.get(pid, {"default_code": None, "name": None, "display": "—", "standard_price": None}) for pid in product_ids_u}

def sync_product_templates_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "product.template"
    table = TB_PRODUCT_TEMPLATES

    desired = [
        "id",
        "name",
        "default_code",
        "barcode",
        "active",
        "type",
        "uom_id",
        "uom_po_id",
        "categ_id",
        "standard_price",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)

    domain: list = []
    last = sb_get_max_write_date(table)
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    ctx = {"active_test": False}

    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            tid = int(r["id"])

            uom_id, uom_name = m2o(r.get("uom_id"))
            uom_po_id, uom_po_name = m2o(r.get("uom_po_id"))
            categ_id, categ_name = m2o(r.get("categ_id"))

            rows.append({
                "odoo_id": tid,
                "name": (r.get("name") or "").strip() or None,
                "default_code": (r.get("default_code") or "").strip() or None,
                "barcode": (r.get("barcode") or "").strip() or None,
                "active": r.get("active"),
                "type": r.get("type"),
                "uom_id": uom_id,
                "uom_name": uom_name,
                "uom_po_id": uom_po_id,
                "uom_po_name": uom_po_name,
                "categ_id": categ_id,
                "categ_name": categ_name,
                "standard_price": r.get("standard_price"),
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ product_templates incremental: {total} filas")
    return total

def backfill_product_templates_from_products(odoo: OdooClient, chunk_ids: int = 300) -> int:
    """
    Inserta/actualiza product_templates para los product_tmpl_id que existen en product_products,
    pero aún no están en product_templates.
    Útil para primer poblamiento rápido.
    """
    # 1) tomar templates referenciados desde variants
    res = (
        sb.table(TB_PRODUCTS)
        .select("product_tmpl_id")
        .not_.is_("product_tmpl_id", "null")
        .limit(20000)
        .execute()
    )
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"Supabase error leyendo {TB_PRODUCTS} product_tmpl_id: {err}")

    tmpl_ids = uniq_ints([r.get("product_tmpl_id") for r in (getattr(res, "data", None) or [])])
    if not tmpl_ids:
        print("✅ product_templates backfill: no hay tmpl_ids en product_products")
        return 0

    # 2) filtrar los que ya existen en product_templates
    # (traemos existentes por chunks para no meter IN gigante)
    existing: set[int] = set()
    for part in chunked(tmpl_ids, 1000):
        r2 = sb.table(TB_PRODUCT_TEMPLATES).select("odoo_id").in_("odoo_id", part).execute()
        e2 = getattr(r2, "error", None)
        if e2:
            raise RuntimeError(f"Supabase error leyendo {TB_PRODUCT_TEMPLATES}: {e2}")
        for x in (getattr(r2, "data", None) or []):
            existing.add(int(x["odoo_id"]))

    missing = [tid for tid in tmpl_ids if int(tid) not in existing]
    if not missing:
        print("✅ product_templates backfill: ya estaban todos")
        return 0

    # 3) pedirlos a Odoo y upsert
    desired = [
        "id","name","default_code","barcode","active","type",
        "uom_id","uom_po_id","categ_id","write_date"
    ]
    fields = available_fields(odoo, "product.template", desired)

    rows: List[dict] = []
    for part in chunked(missing, chunk_ids):
        batch = odoo.search_read(
            "product.template",
            [["id", "in", part]],
            fields,
            limit=100000,
            offset=0,
            order="id asc",
            context={"active_test": False},
        ) or []
        for r in batch:
            tid = int(r["id"])
            uom_id, uom_name = m2o(r.get("uom_id"))
            uom_po_id, uom_po_name = m2o(r.get("uom_po_id"))
            categ_id, categ_name = m2o(r.get("categ_id"))

            rows.append({
                "odoo_id": tid,
                "name": (r.get("name") or "").strip() or None,
                "default_code": (r.get("default_code") or "").strip() or None,
                "barcode": (r.get("barcode") or "").strip() or None,
                "active": r.get("active"),
                "type": r.get("type"),
                "uom_id": uom_id,
                "uom_name": uom_name,
                "uom_po_id": uom_po_id,
                "uom_po_name": uom_po_name,
                "categ_id": categ_id,
                "categ_name": categ_name,
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

    sb_upsert_basic(TB_PRODUCT_TEMPLATES, rows, on_conflict="odoo_id", batch_size=1000)
    print(f"✅ product_templates backfill desde product_products: {len(rows)}")
    return len(rows)



# =========================
# Incremental: CRM
# =========================
def _clean_char(v):
    """Helper: limpia campos char que Odoo devuelve como False → None"""
    if v is False or v is None:
        return None
    s = str(v).strip()
    if s.lower() in ("false", ""):
        return None
    return s
 
 
def sync_crm_projects_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "crm.lead"
    table = TB_CRM
 
    desired = [
        # ── Core ──
        "id", "name", "write_date", "create_date",
        "won_status", "stage_id",
        "partner_id", "contact_name", "email_from", "phone",
        "user_id", "expected_revenue",
 
        # ── Existentes ──
        "x_studio_postulamos",
        "x_studio_activacin_preingreso",
        "x_studio_fecha_de_activacin_de_preingreso",
        "x_studio_fvc",
        "x_studio_es_una_licitacin",
        "x_studio_ofertado_neto",
        "x_studio_presupuesto_neto",
        "x_studio_fecha_de_adjudicacin",
        "x_studio_fecha_estimada_de_compra",
        "x_studio_oc",
        "x_studio_cantidad_de_vehculos",
        "x_studio_fme",
        "x_studio_efme",
 
        # ── Segmentación (selection) ──
        "x_studio_empresa",
        "x_studio_tipo_de_cliente",
        # ── Segmentación (many2one) ──
        "x_studio_canal",
        "x_studio_subcanal",
 
        # ── Jefe de Proyecto (many2one) ──
        "x_studio_many2one_field_6v0_1ihnr0rrc",
 
        # ── Oferta ──
        "x_studio_chasis_ofertado",          # many2one
        "x_studio_presupuesto_estimado",
 
        # ── Cualificación (selection) ──
        "x_studio_autoridad_en_la_decisin",
        "x_studio_plazo_de_compra",
        "x_studio_compatibilidad_del_cliente",
 
        # ── BANT ──
        "x_studio_fecha_bant",
        "x_studio_bant_resultado",
 
        # ── Cotización / UET ──
        "x_studio_fecha_envio_cotizacion",
        "x_studio_uet",
    ]
    fields = available_fields(odoo, model, desired)
 
    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])
 
    ctx = {"active_test": False}
    total = 0
 
    def sv(v):
        if v is False or v is None or v == "":
            return None
        s = str(v).strip()
        return s if s else None
 
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            stage_id, stage_name = m2o(r.get("stage_id"))
            partner_id, partner_name = m2o(r.get("partner_id"))
            user_id, user_name = m2o(r.get("user_id"))
 
            # ✅ FIX: many2one procesados con m2o(), no sv()
            canal_id, canal_name = m2o(r.get("x_studio_canal"))
            subcanal_id, subcanal_name = m2o(r.get("x_studio_subcanal"))
            chasis_id, chasis_name = m2o(r.get("x_studio_chasis_ofertado"))
            jefe_id, jefe_name = m2o(r.get("x_studio_many2one_field_6v0_1ihnr0rrc"))
 
            full_name = (r.get("name") or "").strip()
            identificacion = full_name.split(" ", 1)[0].strip() or None
            mp_code, mp_item_no = parse_mp_from_name(full_name)
 
            rows.append({
                # ── Core ──
                "odoo_id":          int(r["id"]),
                "name":             full_name or None,
                "won_status":       r.get("won_status"),
                "stage_id":         stage_id,
                "stage_name":       stage_name,
                "partner_id":       partner_id,
                "partner_name":     partner_name,
                # ✅ FIX: char fields con _clean_char (False → None en vez de "false")
                "contact_name":     _clean_char(r.get("contact_name")),
                "email_from":       _clean_char(r.get("email_from")),
                "phone":            _clean_char(r.get("phone")),
                "user_id":          user_id,
                "user_name":        user_name,
                "expected_revenue": r.get("expected_revenue"),
                "create_date":      parse_odoo_dt(r.get("create_date")),
                "write_date":       parse_odoo_dt(r.get("write_date")),
                "identificacion":   identificacion,
                "mp_tender_code":   mp_code,
                "mp_item_no":       mp_item_no,
 
                # ── Existentes ──
                "x_studio_cantidad_de_vehculos":             r.get("x_studio_cantidad_de_vehculos"),
                "x_studio_ofertado_neto":                    r.get("x_studio_ofertado_neto"),
                "x_studio_presupuesto_neto":                 r.get("x_studio_presupuesto_neto"),
                "x_studio_fecha_de_adjudicacin":             parse_odoo_date(r.get("x_studio_fecha_de_adjudicacin")),
                "x_studio_fecha_estimada_de_compra":         parse_odoo_date(r.get("x_studio_fecha_estimada_de_compra")),
                "x_studio_oc":                               parse_odoo_bool(r.get("x_studio_oc")),
                "x_studio_postulamos":                       parse_odoo_bool(r.get("x_studio_postulamos")),
                "x_studio_activacin_preingreso":             parse_odoo_bool(r.get("x_studio_activacin_preingreso")),
                "x_studio_fecha_de_activacin_de_preingreso": parse_odoo_dt(r.get("x_studio_fecha_de_activacin_de_preingreso")),
                "x_studio_fvc":                              parse_odoo_date(r.get("x_studio_fvc")),
                "x_studio_es_una_licitacin":                 r.get("x_studio_es_una_licitacin"),
                "x_studio_fme":                              parse_odoo_dt(r.get("x_studio_fme")),
                "x_studio_efme":                             parse_odoo_dt(r.get("x_studio_efme")),
 
                # ── Segmentación (selection siguen con sv) ──
                "x_studio_empresa":          sv(r.get("x_studio_empresa")),
                "x_studio_tipo_de_cliente":  sv(r.get("x_studio_tipo_de_cliente")),
 
                # ✅ FIX: many2one guardan SOLO el nombre
                "x_studio_canal":            canal_name,
                "x_studio_subcanal":         subcanal_name,
                "x_studio_chasis_ofertado":  chasis_name,
 
                # ── Jefe de Proyecto ──
                "x_studio_jefe_proyecto_id":   jefe_id,
                "x_studio_jefe_proyecto_name": jefe_name,
 
                # ── Oferta ──
                "x_studio_presupuesto_estimado": r.get("x_studio_presupuesto_estimado") or None,
 
                # ── Cualificación (selection) ──
                "x_studio_autoridad_en_la_decisin":    sv(r.get("x_studio_autoridad_en_la_decisin")),
                "x_studio_plazo_de_compra":            sv(r.get("x_studio_plazo_de_compra")),
                "x_studio_compatibilidad_del_cliente": sv(r.get("x_studio_compatibilidad_del_cliente")),
 
                # ── BANT ──
                "x_studio_fecha_bant":       parse_odoo_date(r.get("x_studio_fecha_bant")),
                "x_studio_bant_resultado":   sv(r.get("x_studio_bant_resultado")),
 
                # ── Cotización / UET ──
                "x_studio_fecha_envio_cotizacion": parse_odoo_date(r.get("x_studio_fecha_envio_cotizacion")),
                "x_studio_uet":                    r.get("x_studio_uet") or None,
            })
 
        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)
 
    print(f"✅ CRM incremental: {total} filas (desde write_date>{last})")
    return total


def backfill_crm_nuevos_campos(
    odoo: OdooClient,
    chunk_ids: int = 300,
    limit_ids: int = 50_000,
) -> int:
    """
    Backfill de los campos nuevos del área privada.
 
    ✅ FIX: ahora también trae los registros que tienen STRINGS CRUDOS
       (formato "[N, '...']") en canal/subcanal/chasis_ofertado —
       legado del bug que los procesaba con sv() en vez de m2o().
    """
    # 1) IDs con null O con strings crudos
    res = (
        sb.table(TB_CRM)
          .select("odoo_id")
          .or_(
              "x_studio_empresa.is.null,"
              "x_studio_tipo_de_cliente.is.null,"
              "x_studio_canal.is.null,"
              "x_studio_canal.like.[%,"              # ✅ NUEVO: string crudo
              "x_studio_subcanal.like.[%,"           # ✅ NUEVO
              "x_studio_chasis_ofertado.like.[%,"    # ✅ NUEVO
              "x_studio_bant_resultado.is.null,"
              "x_studio_fecha_envio_cotizacion.is.null"
          )
          .limit(limit_ids)
          .execute()
    )
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"Supabase error leyendo CRM null/crudo: {err}")
 
    ids = uniq_ints([r["odoo_id"] for r in (getattr(res, "data", None) or [])])
    if not ids:
        print("✅ crm_projects backfill: nada que actualizar")
        return 0
 
    print(f"🔄 crm_projects backfill: {len(ids)} candidatos detectados")
 
    desired = [
        "id", "write_date",
        "x_studio_empresa", "x_studio_tipo_de_cliente",
        "x_studio_canal", "x_studio_subcanal",
        "x_studio_many2one_field_6v0_1ihnr0rrc",
        "x_studio_chasis_ofertado", "x_studio_presupuesto_estimado",
        "x_studio_autoridad_en_la_decisin", "x_studio_plazo_de_compra",
        "x_studio_compatibilidad_del_cliente",
        "x_studio_fecha_bant", "x_studio_bant_resultado",
        "x_studio_fecha_envio_cotizacion", "x_studio_uet",
    ]
    fields = available_fields(odoo, "crm.lead", desired)
 
    def sv(v):
        if v is False or v is None or v == "":
            return None
        s = str(v).strip()
        return s if s else None
 
    updated = 0
    for part in chunked(ids, chunk_ids):
        batch = odoo.search_read(
            "crm.lead", [["id", "in", part]], fields,
            limit=100_000, offset=0, order="id asc",
            context={"active_test": False},
        ) or []
 
        rows: List[dict] = []
        for r in batch:
            # ✅ FIX: many2one con m2o()
            canal_id, canal_name = m2o(r.get("x_studio_canal"))
            subcanal_id, subcanal_name = m2o(r.get("x_studio_subcanal"))
            chasis_id, chasis_name = m2o(r.get("x_studio_chasis_ofertado"))
            jefe_id, jefe_name = m2o(r.get("x_studio_many2one_field_6v0_1ihnr0rrc"))
 
            rows.append({
                "odoo_id":                              int(r["id"]),
                "write_date":                           parse_odoo_dt(r.get("write_date")),
                "x_studio_empresa":                     sv(r.get("x_studio_empresa")),
                "x_studio_tipo_de_cliente":             sv(r.get("x_studio_tipo_de_cliente")),
 
                # ✅ FIX: many2one → solo nombre
                "x_studio_canal":                       canal_name,
                "x_studio_subcanal":                    subcanal_name,
                "x_studio_chasis_ofertado":             chasis_name,
 
                "x_studio_jefe_proyecto_id":            jefe_id,
                "x_studio_jefe_proyecto_name":          jefe_name,
                "x_studio_presupuesto_estimado":        r.get("x_studio_presupuesto_estimado") or None,
                "x_studio_autoridad_en_la_decisin":     sv(r.get("x_studio_autoridad_en_la_decisin")),
                "x_studio_plazo_de_compra":             sv(r.get("x_studio_plazo_de_compra")),
                "x_studio_compatibilidad_del_cliente":  sv(r.get("x_studio_compatibilidad_del_cliente")),
                "x_studio_fecha_bant":                  parse_odoo_date(r.get("x_studio_fecha_bant")),
                "x_studio_bant_resultado":              sv(r.get("x_studio_bant_resultado")),
                "x_studio_fecha_envio_cotizacion":      parse_odoo_date(r.get("x_studio_fecha_envio_cotizacion")),
                "x_studio_uet":                         r.get("x_studio_uet") or None,
            })
 
        if rows:
            sb_upsert_basic(TB_CRM, rows, on_conflict="odoo_id", batch_size=1000)
            updated += len(rows)
            print(f"  ↳ chunk: {len(rows)} filas (acumulado {updated}/{len(ids)})")
 
    print(f"✅ crm_projects backfill: {updated} filas actualizadas")
    return updated
    

def backfill_crm_fme_efme(
    odoo: OdooClient,
    chunk_ids: int = 300,
    limit_ids: int = 20000,
) -> int:
    """
    Backfill de x_studio_fme / x_studio_efme en crm_projects,
    para registros antiguos que quedaron NULL al agregar columnas.

    - Lee desde Supabase los odoo_id que tengan fme o efme en NULL.
    - Trae esos leads desde Odoo por ID.
    - Upsert SOLO esas columnas + write_date (y opcionalmente name para debug).
    """
    # 1) buscar ids faltantes en Supabase (OR)
    res = (
        sb.table(TB_CRM)
          .select("odoo_id")
          .or_("x_studio_fme.is.null,x_studio_efme.is.null")
          .limit(limit_ids)
          .execute()
    )
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"Supabase error leyendo {TB_CRM} null fme/efme: {err}")

    ids = [int(r["odoo_id"]) for r in (getattr(res, "data", None) or []) if r.get("odoo_id") is not None]
    ids = uniq_ints(ids)

    if not ids:
        print("✅ crm_projects: no hay FME/EFME nulos (backfill no requerido)")
        return 0

    # 2) traer desde Odoo solo campos necesarios
    desired = ["id", "write_date", "x_studio_fme", "x_studio_efme"]
    # si quieres debug:
    # desired = ["id", "name", "write_date", "x_studio_fme", "x_studio_efme"]

    fields = available_fields(odoo, "crm.lead", desired)

    updated = 0
    for part in chunked(ids, chunk_ids):
        batch = odoo.search_read(
            "crm.lead",
            [["id", "in", part]],
            fields,
            limit=100000,
            offset=0,
            order="id asc",
            context={"active_test": False},
        ) or []

        rows: List[dict] = []
        for r in batch:
            rows.append({
                "odoo_id": int(r["id"]),
                "write_date": parse_odoo_dt(r.get("write_date")),
                # ✅ AJUSTA ESTO SEGÚN EL TIPO REAL EN ODOO:
                # Si son datetime:
                "x_studio_fme": parse_odoo_dt(r.get("x_studio_fme")),
                "x_studio_efme": parse_odoo_dt(r.get("x_studio_efme")),
                # Si son date, reemplaza por:
                # "x_studio_fme": parse_odoo_date(r.get("x_studio_fme")),
                # "x_studio_efme": parse_odoo_date(r.get("x_studio_efme")),
            })

        if rows:
            sb_upsert_basic(TB_CRM, rows, on_conflict="odoo_id", batch_size=1000)
            updated += len(rows)

    print(f"✅ crm_projects backfill fme/efme: {updated} filas actualizadas (sobre {len(ids)} ids candidatos)")
    return updated



# =========================
# Analitycs Account
# =========================

def sync_account_analytic_accounts_full(odoo: OdooClient, chunk: int = 800) -> int:
    """
    Full sync (recomendado): account.analytic.account con debit/credit/balance.
    NO incremental por write_date porque los saldos pueden cambiar sin cambiar write_date.
    """
    model = "account.analytic.account"
    table = TB_ANALYTIC_ACCOUNTS

    desired = [
        "id",
        "name",
        "code",
        "active",
        "company_id",
        "partner_id",
        "currency_id",
        "debit",
        "credit",
        "balance",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)

    # Full: traemos todo (incluye inactivos)
    domain: list = []
    ctx = {"active_test": False}

    total = 0
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            company_id, company_name = m2o(r.get("company_id"))
            partner_id, partner_name = m2o(r.get("partner_id"))
            currency_id, currency_name = m2o(r.get("currency_id"))

            rows.append({
                "odoo_id": int(r["id"]),
                "name": (r.get("name") or "").strip() or None,
                "code": (r.get("code") or "").strip() or None,
                "active": r.get("active"),
                "company_id": company_id,
                "company_name": company_name,
                "partner_id": partner_id,
                "partner_name": partner_name,
                "currency_id": currency_id,
                "currency_name": currency_name,
                "debit": r.get("debit"),
                "credit": r.get("credit"),
                "balance": r.get("balance"),
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ account_analytic_accounts full: {total} filas")
    return total


# =========================
# Incremental: Sales Notes
# =========================
def sync_sales_notes_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "sale.order"
    table = TB_SALES

    desired = ["id", "name", "state", "date_order", "amount_total", "opportunity_id", "partner_id", "write_date"]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk):
        rows = []
        for r in batch:
            opp_id, _ = m2o(r.get("opportunity_id"))
            partner_id, partner_name = m2o(r.get("partner_id"))
            rows.append({
                "odoo_id": int(r["id"]),
                "name": r.get("name"),
                "state": r.get("state"),
                "date_order": parse_odoo_dt(r.get("date_order")),
                "amount_total": r.get("amount_total"),
                "opportunity_id": opp_id,
                "partner_id": partner_id,
                "partner_name": partner_name,
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ Sales notes incremental: {total} filas (desde write_date>{last})")
    return total


def sync_sale_order_lines_incremental(odoo: OdooClient, chunk: int = 1200) -> int:
    model = "sale.order.line"
    table = TB_SALE_ORDER_LINES

    desired = [
        "id","order_id","state",
        "product_id","name","product_uom_qty","qty_delivered","qty_invoiced",
        "price_unit","price_subtotal","price_total",
        "currency_id","company_id",
        "analytic_distribution",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context={"active_test": False}):

        # ✅ 1) antes de insertar líneas: asegurar que existan los sale.order (headers)
        batch_order_ids: List[int] = []
        for r in batch:
            oid, _ = m2o(r.get("order_id"))
            if oid:
                batch_order_ids.append(int(oid))

        # ✅ 2) backfill de headers faltantes en sales_notes (evita FK)
        ensured = ensure_sales_orders_exist(odoo, batch_order_ids)
        if ensured:
            print(f"ℹ️ Backfill sales_notes: {ensured} headers insertados/actualizados para cumplir FK")

        # ✅ 3) ahora sí: armar rows de líneas
        rows = []
        for r in batch:
            order_id, order_name = m2o(r.get("order_id"))
            product_id, product_name = m2o(r.get("product_id"))
            currency_id, currency_name = m2o(r.get("currency_id"))
            company_id, company_name = m2o(r.get("company_id"))

            ad = r.get("analytic_distribution")
            if ad is False:
                ad = None

            rows.append({
                "odoo_id": int(r["id"]),
                "order_id": order_id,
                "order_name": order_name,
                "state": r.get("state"),
                "product_id": product_id,
                "product_name": product_name,
                "line_name": r.get("name"),
                "product_uom_qty": r.get("product_uom_qty"),
                "qty_delivered": r.get("qty_delivered"),
                "qty_invoiced": r.get("qty_invoiced"),
                "price_unit": r.get("price_unit"),
                "price_subtotal": r.get("price_subtotal"),
                "price_total": r.get("price_total"),
                "currency_id": currency_id,
                "currency_name": currency_name,
                "company_id": company_id,
                "company_name": company_name,
                "analytic_distribution": ad,
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ sale_order_lines incremental: {total} filas (desde write_date>{last})")
    return total


def backfill_missing_account_moves(odoo: OdooClient, limit_ids: int = 20000, chunk_ids: int = 300) -> int:
    """
    Backfill de account.move (headers) faltantes, detectados por move_id en account_move_lines
    que no existen en account_moves.
    """
    # 1) Traer move_ids faltantes desde Supabase
    # OJO: esto requiere que account_move_lines tenga move_id poblado.
    res = (
        sb.rpc("rpc_missing_account_moves", {"limit_ids": limit_ids}).execute()
        if False else None
    )

    # Sin RPC: usamos una consulta simple con PostgREST (2 pasos)
    # (a) traer move_ids distintos desde AML (limitado)
    r1 = sb.table(TB_ACCOUNT_MOVE_LINES).select("move_id").not_.is_("move_id", "null").limit(limit_ids).execute()
    e1 = getattr(r1, "error", None)
    if e1:
        raise RuntimeError(f"Supabase error leyendo {TB_ACCOUNT_MOVE_LINES}.move_id: {e1}")
    move_ids = uniq_ints([x.get("move_id") for x in (getattr(r1, "data", None) or [])])

    if not move_ids:
        print("✅ backfill account_moves: no hay move_ids en account_move_lines")
        return 0

    # (b) filtrar los que ya existen en account_moves
    existing: set[int] = set()
    for part in chunked(move_ids, 1000):
        r2 = sb.table(TB_ACCOUNT_MOVES).select("odoo_id").in_("odoo_id", part).execute()
        e2 = getattr(r2, "error", None)
        if e2:
            raise RuntimeError(f"Supabase error leyendo {TB_ACCOUNT_MOVES}: {e2}")
        for x in (getattr(r2, "data", None) or []):
            existing.add(int(x["odoo_id"]))

    missing = [mid for mid in move_ids if int(mid) not in existing]
    if not missing:
        print("✅ backfill account_moves: ya estaban todos")
        return 0

    # 2) Traer desde Odoo y upsert
    desired = [
        "id","name","ref",
        "move_type","state",
        "invoice_date","date","invoice_date_due",
        "partner_id","journal_id","company_id",
        "currency_id",
        "amount_untaxed","amount_tax","amount_total",
        "payment_state",
        "invoice_origin",
        "write_date",
    ]
    fields = available_fields(odoo, "account.move", desired)
    ctx = {"active_test": False}

    total = 0
    rows: List[dict] = []

    for part in chunked(missing, chunk_ids):
        batch = odoo.search_read("account.move", [["id", "in", part]], fields, limit=100000, offset=0, order="id asc", context=ctx) or []
        for r in batch:
            partner_id, partner_name = m2o(r.get("partner_id"))
            journal_id, journal_name = m2o(r.get("journal_id"))
            company_id, company_name = m2o(r.get("company_id"))
            currency_id, currency_name = m2o(r.get("currency_id"))

            rows.append({
                "odoo_id": int(r["id"]),
                "name": r.get("name"),
                "ref": r.get("ref"),
                "move_type": r.get("move_type"),
                "state": r.get("state"),
                "invoice_date": parse_odoo_date(r.get("invoice_date")),
                "date": parse_odoo_date(r.get("date")),
                "invoice_date_due": parse_odoo_date(r.get("invoice_date_due")),
                "partner_id": partner_id,
                "partner_name": partner_name,
                "journal_id": journal_id,
                "journal_name": journal_name,
                "company_id": company_id,
                "company_name": company_name,
                "currency_id": currency_id,
                "currency_name": currency_name,
                "amount_untaxed": r.get("amount_untaxed"),
                "amount_tax": r.get("amount_tax"),
                "amount_total": r.get("amount_total"),
                "payment_state": r.get("payment_state"),
                "invoice_origin": r.get("invoice_origin"),
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

        if rows:
            sb_upsert_basic(TB_ACCOUNT_MOVES, rows, on_conflict="odoo_id", batch_size=1000)
            total += len(rows)
            rows = []

    print(f"✅ backfill account_moves: {total} headers insertados")
    return total

# =========================
# Incremental: Manufacturing Orders
# =========================
CTX_ALL = {"active_test": False}

def sync_manufacturing_orders_incremental(odoo: OdooClient, chunk: int = 800,
                                          full: bool = False, run_ts_iso: Optional[str] = None) -> Tuple[List[int], List[int], List[int]]:
    model = "mrp.production"
    table = TB_MO

    desired = [
        "id", "name", "origin", "state",
        "product_id", "product_qty",
        "date_start", "date_finished",
        "create_date",                                  # ← NUEVO
        "write_date", "bom_id", "procurement_group_id", "latest_bom_id",
    ]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last and not full:                               # full=True → trae TODO lo vivo (para reconciliar bajas)
        domain.append(["write_date", ">", last])
 
    total = 0
    mo_ids, bom_ids, group_ids = [], [], []
 
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=CTX_ALL):
        rows = []
        for r in batch:
            mo_id = int(r["id"])
            mo_ids.append(mo_id)
 
            product_id, product_name = m2o(r.get("product_id"))
            bom_id, _ = m2o(r.get("bom_id"))
            latest_bom_id, _ = m2o(r.get("latest_bom_id"))
            pg_id, _ = m2o(r.get("procurement_group_id"))
 
            if bom_id:
                bom_ids.append(bom_id)
            if latest_bom_id:
                bom_ids.append(latest_bom_id)
            if pg_id:
                group_ids.append(pg_id)
 
            row = {
                "odoo_id": mo_id,
                "name": r.get("name"),
                "origin": r.get("origin"),
                "state": r.get("state"),
                "product_id": product_id,
                "product_name": product_name,
                "qty": r.get("product_qty"),
                "bom_id": bom_id,
                "latest_bom_id": latest_bom_id,
                "procurement_group_id": pg_id,
                "date_start": parse_odoo_dt(r.get("date_start")),
                "date_finished": parse_odoo_dt(r.get("date_finished")),
                "create_date": parse_odoo_dt(r.get("create_date")),   # ← NUEVO
                "write_date": parse_odoo_dt(r.get("write_date")),
            }
            # Reconciliación de bajas: en la pasada full estampamos last_seen_at y
            # reactivamos (una fila vista sigue viva). Las no vistas las marca la RPC.
            if full and run_ts_iso:
                row["last_seen_at"] = run_ts_iso
                row["is_active"] = True
                row["missing_since"] = None
            rows.append(row)

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ MO {'FULL' if full else 'incremental'}: {total} filas (desde write_date>{last if not full else 'ALL'})")
    return uniq_ints(mo_ids), uniq_ints(bom_ids), uniq_ints(group_ids)


# ─────────────────────────────────────────────────────────────────────────
# (1b) Backfill create_date para las OF ya existentes (idempotente).
#      NUEVA función. Llamar 1 vez (o dejar en main: hace no-op cuando termina).
# ─────────────────────────────────────────────────────────────────────────
def backfill_manufacturing_orders_create_date(odoo: OdooClient, chunk_ids: int = 300, limit_ids: int = 50000) -> int:
    res = (
        sb.table(TB_MO)
          .select("odoo_id")
          .is_("create_date", "null")
          .limit(limit_ids)
          .execute()
    )
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"Supabase error leyendo {TB_MO} create_date null: {err}")
 
    ids = uniq_ints([r["odoo_id"] for r in (getattr(res, "data", None) or [])])
    if not ids:
        print("✅ manufacturing_orders backfill create_date: nada que actualizar")
        return 0
 
    desired = ["id", "create_date", "write_date"]
    fields = available_fields(odoo, "mrp.production", desired)
 
    updated = 0
    for part in chunked(ids, chunk_ids):
        batch = odoo.search_read(
            "mrp.production", [["id", "in", part]], fields,
            limit=100000, offset=0, order="id asc",
            context={"active_test": False},
        ) or []
        rows = [{
            "odoo_id": int(r["id"]),
            "create_date": parse_odoo_dt(r.get("create_date")),
            "write_date": parse_odoo_dt(r.get("write_date")),
        } for r in batch]
        if rows:
            sb_upsert_basic(TB_MO, rows, on_conflict="odoo_id", batch_size=1000)
            updated += len(rows)
 
    print(f"✅ manufacturing_orders backfill create_date: {updated} filas sobre {len(ids)} candidatos")
    return updated    

# =========================
# Incremental: BOM + BOM lines por bom_ids
# =========================
def sync_bom_and_lines_by_ids(odoo: OdooClient, bom_ids: List[int], chunk_ids: int = 300) -> None:
    if not bom_ids:
        print("ℹ️ No BOM IDs incrementales.")
        return

    # BOM
    bom_fields_desired = [
        "id", "code", "active", "type",
        "product_tmpl_id", "product_id",
        "product_qty", "product_uom_id",
        "company_id", "write_date",
    ]
    bom_fields = available_fields(odoo, "mrp.bom", bom_fields_desired)

    bom_rows: List[dict] = []
    for part in chunked(bom_ids, chunk_ids):
        domain = [["id", "in", part]]
        batch = odoo.search_read("mrp.bom", domain, bom_fields, limit=100000, offset=0)
        for r in batch:
            product_tmpl_id, product_tmpl_name = m2o(r.get("product_tmpl_id"))
            product_id, product_name = m2o(r.get("product_id"))
            uom_id, uom_name = m2o(r.get("product_uom_id"))
            company_id, company_name = m2o(r.get("company_id"))

            bom_rows.append({
                "odoo_id": int(r["id"]),
                "code": r.get("code"),
                "active": r.get("active"),
                "type": r.get("type"),
                "product_tmpl_id": product_tmpl_id,
                "product_tmpl_name": product_tmpl_name,
                "product_id": product_id,
                "product_name": product_name,
                "product_qty": r.get("product_qty"),
                "product_uom_id": uom_id,
                "product_uom_name": uom_name,
                "company_id": company_id,
                "company_name": company_name,
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

    sb_upsert_basic(TB_BOM, bom_rows, on_conflict="odoo_id", batch_size=1000)
    print(f"✅ BOM upsert (por ids): {len(bom_rows)}")

    # BOM lines
    line_fields_desired = ["id", "bom_id", "product_id", "product_qty", "product_uom_id", "operation_id", "child_bom_id", "write_date"]
    line_fields = available_fields(odoo, "mrp.bom.line", line_fields_desired)

    line_rows: List[dict] = []
    for part in chunked(bom_ids, chunk_ids):
        domain = [["bom_id", "in", part]]
        batch = odoo.search_read("mrp.bom.line", domain, line_fields, limit=100000, offset=0)
        for r in batch:
            bom_id, bom_name = m2o(r.get("bom_id"))
            product_id, product_name = m2o(r.get("product_id"))
            uom_id, uom_name = m2o(r.get("product_uom_id"))
            child_bom_id, _ = m2o(r.get("child_bom_id"))
            op_id, op_name = m2o(r.get("operation_id"))

            line_rows.append({
                "odoo_id": int(r["id"]),
                "bom_id": bom_id,
                "bom_name": bom_name,
                "product_id": product_id,
                "product_name": product_name,
                "product_qty": r.get("product_qty"),
                "product_uom_id": uom_id,
                "product_uom_name": uom_name,
                "operation_id": op_id,
                "operation_name": op_name,
                "child_bom_id": child_bom_id,
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

    sb_upsert_basic(TB_BOM_LINES, line_rows, on_conflict="odoo_id", batch_size=1000)
    print(f"✅ BOM lines upsert (por bom_ids): {len(line_rows)}")

# =========================
# Incremental: Pickings / Moves / MoveLines (pesadas) via RPC + HASH
# =========================
def sync_picking_batches_incremental(odoo: OdooClient, run_ts_iso: str,
                                     chunk: int = 500, full: bool = False) -> int:
    """Mirror de stock.picking.batch (lotes de traslado BATCH/xxxxx y waves).

    Modelo liviano: decenas de filas nuevas por semana. El link picking->batch
    se sincroniza aparte en sync_pickings_incremental (columna batch_id).
    """
    model = "stock.picking.batch"
    table = TB_PICKING_BATCHES

    fields_desired = [
        "id", "name", "state", "is_wave",
        "user_id", "company_id", "picking_type_id",
        "scheduled_date", "date_done", "description",
        "create_date", "write_date",
    ]
    fields = available_fields(odoo, model, fields_desired)
    ctx = {"active_test": False}

    last = sb_get_max_write_date(table)
    domain: list = []
    if last and not full:
        domain.append(["write_date", ">", last])

    total_rows = 0
    affected = 0

    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            bid = int(r["id"])
            uid, uname = m2o(r.get("user_id"))
            cid, _ = m2o(r.get("company_id"))
            pt_id, pt_name = m2o(r.get("picking_type_id"))

            row = {
                "odoo_id": bid,
                "name": r.get("name"),
                "state": r.get("state"),
                "is_wave": bool(r.get("is_wave")),
                "user_id": uid,
                "user_name": uname,
                "company_id": cid,
                "picking_type_id": pt_id,
                "picking_type_name": pt_name,
                "scheduled_date": parse_odoo_dt(r.get("scheduled_date")),
                "date_done": parse_odoo_dt(r.get("date_done")),
                "description": r.get("description") or None,
                "create_date": parse_odoo_dt(r.get("create_date")),
                "write_date": parse_odoo_dt(r.get("write_date")),
                "last_seen_at": run_ts_iso,
                "is_active": True,
                "missing_since": None,
            }

            row["row_hash"] = make_hash(row, [
                "odoo_id", "name", "state", "is_wave",
                "user_id", "user_name", "company_id",
                "picking_type_id", "picking_type_name",
                "scheduled_date", "date_done", "description",
                "create_date", "write_date", "is_active", "missing_since",
            ])

            rows.append(row)

        total_rows += len(rows)
        affected += sb_rpc_upsert("rpc_upsert_stock_picking_batches", rows,
                                  batch_size=BATCH_PBATCHES)

    modo = "FULL" if full else "incremental"
    print(f"✅ PickingBatches {modo}: fetched={total_rows} | db_affected={affected} "
          f"(desde write_date>{last if not full else 'TODO'})")
    return total_rows


def sync_pickings_incremental(odoo: OdooClient, run_ts_iso: str, chunk: int = 1200, full: bool = False) -> int:
    model = "stock.picking"
    table = TB_PICKINGS
 
    pick_fields_desired = [
        "id", "name", "state", "origin",
        "x_studio_of_primaria",
        "x_studio_mdulo",
        "scheduled_date", "date_done",
        "create_date",
        "picking_type_id",
        "location_id", "location_dest_id",
        "group_id",
        "write_date", "return_id",
        "batch_id"
    ]
    pick_fields = available_fields(odoo, model, pick_fields_desired)
    ctx = {"active_test": False}
 
    last = sb_get_max_write_date(table)
    domain: list = []
    if last and not full:                               # ← full salta el filtro write_date
        domain.append(["write_date", ">", last])
 
    pt_map: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    try:
        pt_batch = odoo.search_read("stock.picking.type", [], ["id", "code", "name"], limit=100000, offset=0)
        pt_map = {int(r["id"]): (r.get("code"), r.get("name")) for r in (pt_batch or [])}
    except Exception:
        pt_map = {}
 
    total_rows = 0
    affected = 0
 
    for batch in iter_search_read_all(odoo, model, domain, pick_fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            pid = int(r["id"])
            pt_id, pt_name_disp = m2o(r.get("picking_type_id"))
            pt_code = None
            pt_name = None
            if pt_id and pt_id in pt_map:
                pt_code, pt_name = pt_map[pt_id]
            else:
                pt_name = pt_name_disp
 
            loc_id, loc_name = m2o(r.get("location_id"))
            locd_id, locd_name = m2o(r.get("location_dest_id"))
            gid, _ = m2o(r.get("group_id"))
 
            x_ofp = r.get("x_studio_of_primaria")
            ofp_id, ofp_name = m2o(r.get("x_studio_of_primaria"))
            ret_id, ret_name = m2o(r.get("return_id"))
            bat_id, bat_name = m2o(r.get("batch_id"))
 
            mod_id = None
            mod_name = None
            if "x_studio_modulo" in r:
                mod_id, mod_name = m2o(r.get("x_studio_modulo"))
            elif "x_studio_mdulo" in r:
                mod_id, mod_name = m2o(r.get("x_studio_mdulo"))
 
            row = {
                "odoo_id": pid,
                "name": r.get("name"),
                "state": r.get("state"),
                "origin": r.get("origin"),
                "scheduled_date": parse_odoo_dt(r.get("scheduled_date")),
                "date_done": parse_odoo_dt(r.get("date_done")),
                "create_date": parse_odoo_dt(r.get("create_date")),
                "picking_type_id": pt_id,
                "picking_type_name": pt_name,
                "picking_type_code": pt_code,
                "location_id": loc_id,
                "location_name": loc_name,
                "location_dest_id": locd_id,
                "location_dest_name": locd_name,
                "group_id": gid,
                "x_studio_modulo_id": mod_id,
                "x_studio_modulo_name": mod_name,
                "x_studio_of_primaria": x_ofp,
                "x_studio_of_primaria_id": ofp_id,
                "x_studio_of_primaria_name": ofp_name,
                "write_date": parse_odoo_dt(r.get("write_date")),
                "last_seen_at": run_ts_iso,
                "is_active": True,
                "missing_since": None,
                "return_id": ret_id,
                "batch_id": bat_id,
                "batch_name": bat_name,
            }
 
            row["row_hash"] = make_hash(row, [
                "odoo_id", "name", "state", "origin",
                "scheduled_date", "date_done", "create_date",
                "picking_type_id", "picking_type_name", "picking_type_code",
                "location_id", "location_name", "location_dest_id", "location_dest_name",
                "group_id", "x_studio_modulo_id", "x_studio_modulo_name",
                "x_studio_of_primaria", "x_studio_of_primaria_id", "x_studio_of_primaria_name",
                "write_date", "is_active", "missing_since", "return_id",
                "batch_id", "batch_name"
            ])
 
            rows.append(row)
 
        total_rows += len(rows)
        affected += sb_rpc_upsert("rpc_upsert_stock_pickings", rows, batch_size=BATCH_PICKINGS)
 
    modo = "FULL" if full else "incremental"
    print(f"✅ Pickings {modo}: fetched={total_rows} | db_affected={affected} (desde write_date>{last if not full else 'TODO'})")
    return total_rows



def sync_moves_incremental(odoo: OdooClient, run_ts_iso: str, chunk: int = 1500, full: bool = False) -> int:
    model = "stock.move"
    table = TB_MOVES
 
    move_fields_desired = [
        "id", "name", "picking_id",
        "product_id", "product_uom_qty", "quantity", "product_uom",
        "state", "location_id", "location_dest_id",
        "date", "reference", "group_id",
        "raw_material_production_id", "production_id",
        "bom_line_id",                                  # ← NUEVO
        "write_date",
    ]
    move_fields = available_fields(odoo, model, move_fields_desired)
 
    last = sb_get_max_write_date(table)
    domain: list = []
    if last and not full:                               # ← full salta el filtro write_date
        domain.append(["write_date", ">", last])
 
    total_rows = 0
    affected = 0
 
    for batch in iter_search_read_all(odoo, model, domain, move_fields, chunk=chunk, context={"active_test": False}):
        prod_ids_batch: List[int] = []
        for r in batch:
            pid, _ = m2o(r.get("product_id"))
            if pid:
                prod_ids_batch.append(pid)
        prod_map = fetch_product_map(odoo, prod_ids_batch, chunk_ids=300)
 
        rows: List[dict] = []
        for r in batch:
            mid = int(r["id"])
            picking_id, picking_name = m2o(r.get("picking_id"))
 
            product_id, _ = m2o(r.get("product_id"))
            pinfo = prod_map.get(int(product_id or 0), {"display": "—"})
            product_display = pinfo.get("display") or "—"
 
            uom_id, uom_name = m2o(r.get("product_uom"))
            loc_id, loc_name = m2o(r.get("location_id"))
            locd_id, locd_name = m2o(r.get("location_dest_id"))
            gid, _ = m2o(r.get("group_id"))
            rm_mo_id, rm_mo_name = m2o(r.get("raw_material_production_id"))
            prod_mo_id, prod_mo_name = m2o(r.get("production_id"))
            bom_line_id, _ = m2o(r.get("bom_line_id"))   # ← NUEVO
 
            row = {
                "odoo_id": mid,
                "name": r.get("name"),
                "picking_id": picking_id,
                "picking_name": picking_name,
                "product_id": product_id,
                "product_name": product_display,
                "product_uom_qty": r.get("product_uom_qty"),
                "quantity": r.get("quantity") if r.get("quantity") is not None else r.get("product_uom_qty"),
                "product_uom_id": uom_id,
                "product_uom_name": uom_name,
                "state": r.get("state"),
                "location_id": loc_id,
                "location_name": loc_name,
                "location_dest_id": locd_id,
                "location_dest_name": locd_name,
                "date": parse_odoo_dt(r.get("date")),
                "reference": r.get("reference"),
                "group_id": gid,
                "raw_material_production_id": rm_mo_id,
                "raw_material_production_name": rm_mo_name,
                "production_id": prod_mo_id,
                "production_name": prod_mo_name,
                "bom_line_id": bom_line_id,              # ← NUEVO
                "write_date": parse_odoo_dt(r.get("write_date")),
                "last_seen_at": run_ts_iso,
                "is_active": True,
                "missing_since": None,
            }
 
            row["row_hash"] = make_hash(row, [
                "odoo_id", "name", "picking_id", "picking_name",
                "product_id", "product_name", "product_uom_qty", "quantity",
                "product_uom_id", "product_uom_name",
                "state", "location_id", "location_name", "location_dest_id", "location_dest_name",
                "date", "reference", "group_id",
                "raw_material_production_id", "raw_material_production_name",
                "production_id", "production_name",
                "bom_line_id",                           # ← NUEVO (entra al hash)
                "write_date", "is_active", "missing_since"
            ])
 
            rows.append(row)
 
        total_rows += len(rows)
        affected += sb_rpc_upsert("rpc_upsert_stock_moves", rows, batch_size=BATCH_MOVES)
 
    modo = "FULL" if full else "incremental"
    print(f"✅ Moves {modo}: fetched={total_rows} | db_affected={affected} (desde write_date>{last if not full else 'TODO'})")
    return total_rows




def sync_move_lines_incremental(odoo: OdooClient, run_ts_iso: str, chunk: int = 1500, full: bool = False) -> int:
    model = "stock.move.line"
    table = TB_MOVE_LINES
 
    ml_fields_desired = [
        "id", "picking_id", "move_id", "product_id", "qty_done", "product_uom_id",
        "location_id", "location_dest_id", "lot_id", "package_id", "result_package_id",
        "date", "state", "write_date",
    ]
    ml_fields = available_fields(odoo, model, ml_fields_desired)
 
    last = sb_get_max_write_date(table)
    domain: list = []
    if last and not full:                               # ← full salta el filtro write_date
        domain.append(["write_date", ">", last])
 
    total_rows = 0
    affected = 0
 
    for batch in iter_search_read_all(odoo, model, domain, ml_fields, chunk=chunk, context={"active_test": False}):
        prod_ids_batch: List[int] = []
        for r in batch:
            pid, _ = m2o(r.get("product_id"))
            if pid:
                prod_ids_batch.append(pid)
        prod_map = fetch_product_map(odoo, prod_ids_batch, chunk_ids=300)
 
        rows: List[dict] = []
        for r in batch:
            picking_id, picking_name = m2o(r.get("picking_id"))
            move_id, move_name = m2o(r.get("move_id"))
 
            product_id, _ = m2o(r.get("product_id"))
            pinfo = prod_map.get(int(product_id or 0), {"display": "—", "standard_price": None})
            product_display = pinfo.get("display") or "—"
            std_price = pinfo.get("standard_price")
 
            uom_id, uom_name = m2o(r.get("product_uom_id"))
            loc_id, loc_name = m2o(r.get("location_id"))
            locd_id, locd_name = m2o(r.get("location_dest_id"))
            lot_id, lot_name = m2o(r.get("lot_id"))
            pkg_id, pkg_name = m2o(r.get("package_id"))
            rpkg_id, rpkg_name = m2o(r.get("result_package_id"))
 
            row = {
                "odoo_id": int(r["id"]),
                "picking_id": picking_id,
                "picking_name": picking_name,
                "move_id": move_id,
                "move_name": move_name,
                "product_id": product_id,
                "product_name": product_display,
                "qty_done": r.get("qty_done"),
                "standard_price_snapshot": std_price,
                "product_uom_id": uom_id,
                "product_uom_name": uom_name,
                "location_id": loc_id,
                "location_name": loc_name,
                "location_dest_id": locd_id,
                "location_dest_name": locd_name,
                "lot_id": lot_id,
                "lot_name": lot_name,
                "package_id": pkg_id,
                "package_name": pkg_name,
                "result_package_id": rpkg_id,
                "result_package_name": rpkg_name,
                "date": parse_odoo_dt(r.get("date")),
                "state": r.get("state"),
                "write_date": parse_odoo_dt(r.get("write_date")),
                "last_seen_at": run_ts_iso,
                "is_active": True,
                "missing_since": None,
            }
 
            row["row_hash"] = make_hash(row, [
                "odoo_id", "picking_id", "picking_name", "move_id", "move_name",
                "product_id", "product_name", "qty_done", "standard_price_snapshot",
                "product_uom_id", "product_uom_name",
                "location_id", "location_name", "location_dest_id", "location_dest_name",
                "lot_id", "lot_name", "package_id", "package_name",
                "result_package_id", "result_package_name",
                "date", "state", "write_date", "is_active", "missing_since"
            ])
 
            rows.append(row)
 
        total_rows += len(rows)
        affected += sb_rpc_upsert("rpc_upsert_stock_move_lines", rows, batch_size=BATCH_MOVE_LINES)
 
    modo = "FULL" if full else "incremental"
    print(f"✅ MoveLines {modo}: fetched={total_rows} | db_affected={affected} (desde write_date>{last if not full else 'TODO'})")
    return total_rows

# ─────────────────────────────────────────────────────────────────────────
# (4) Full-resync diario de tablas de stock + soft-delete.
#     NUEVA función. Trae el universo completo y marca inactivo lo que ya
#     no existe en Odoo (mata filas fantasma). Reutiliza rpc_mark_missing_stock_tables.
# ─────────────────────────────────────────────────────────────────────────
def full_resync_stock_tables(odoo: OdooClient, run_ts_iso: str, soft_delete_days: Optional[int] = None) -> dict:
    days = soft_delete_days if soft_delete_days is not None else FULL_RESYNC_SOFT_DELETE_DAYS
    print(f"🔄 FULL resync stock (pickings/moves/move_lines) | run_ts={run_ts_iso} | days={days}")
 
    fetched = {
        "stock_picking_batches": sync_picking_batches_incremental(odoo, run_ts_iso, chunk=500, full=True),
        "stock_pickings":   sync_pickings_incremental(odoo, run_ts_iso, chunk=1200, full=True),
        "stock_moves":      sync_moves_incremental(odoo, run_ts_iso, chunk=1500, full=True),
        "stock_move_lines": sync_move_lines_incremental(odoo, run_ts_iso, chunk=1500, full=True),
    }
 
    # Soft-delete: marca inactivo lo NO visto en este full fetch.
    # Guard de seguridad: si una tabla devolvió 0 filas (fetch fallido) NO la tocamos,
    # para no desactivar todo por un error de red.
    marked = {}
    for table, n in fetched.items():
        if n <= 0:
            print(f"⚠️ {table}: fetch=0 → se omite soft-delete por seguridad")
            marked[table] = None
            continue
        try:
            res = sb.rpc("rpc_mark_missing_stock_tables",
                         {"table_name": table, "run_ts": run_ts_iso, "days": days}).execute()
            err = getattr(res, "error", None)
            if err:
                print(f"⚠️ soft-delete {table}: {err}")
                marked[table] = None
            else:
                val = getattr(res, "data", None) or 0
                marked[table] = val
                print(f"✅ soft-delete {table}: {val} marcados inactivos")
        except Exception as e:
            print(f"⚠️ soft-delete {table}: {e}")
            marked[table] = None
 
    summary = {"fetched": fetched, "marked_inactive": marked}
    print(f"📊 Resumen full-resync stock: {summary}")
    return summary

# =========================
# Purchase Order Incremental
# =========================

def sync_purchase_orders_incremental(odoo: OdooClient, chunk: int = 800, full: bool = False, run_ts_iso: Optional[str] = None) -> int:
    """
    full=True → ignora write_date y trae todo. Recomendado para PO: effective_date,
    use_approval_route y current_approvers son computed (store=False o recompute sin
    bump de write_date) y solo se mantienen frescos con full. La tabla es chica (~6.3k).
    """
    model = "purchase.order"
    table = TB_PURCHASE_ORDERS

    desired = [
        "id", "name", "state",
        "date_order", "date_approve", "date_planned",
        "effective_date", "real_reception_date",
        "partner_id", "partner_ref",
        "amount_total", "amount_untaxed", "amount_tax",
        "invoice_status", "receipt_status",
        "currency_id", "company_id", "user_id", "origin",
        "payment_term_id",
        "x_studio_estado_de_pago",
        "x_studio_fecha_de_pago_esperada",
        "x_studio_credito",
        # Ruta de aprobación
        "use_approval_route", "is_under_approval",
        "is_fully_approved", "is_approval_received",
        "approval_route_id", "current_approval_stage_id",
        "next_approval_stage_id", "current_approvers",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last and not full:
        domain.append(["write_date", ">", last])

    total = 0
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context={"active_test": False}):
        rows = []
        for r in batch:
            partner_id, partner_name = m2o(r.get("partner_id"))
            currency_id, currency_name = m2o(r.get("currency_id"))
            company_id, company_name = m2o(r.get("company_id"))
            user_id, user_name = m2o(r.get("user_id"))
            payterm_id, payterm_name = m2o(r.get("payment_term_id"))
            route_id, route_name = m2o(r.get("approval_route_id"))
            cur_stage_id, cur_stage_name = m2o(r.get("current_approval_stage_id"))
            nxt_stage_id, nxt_stage_name = m2o(r.get("next_approval_stage_id"))

            # many2many → lista de ids (o None)
            approvers = r.get("current_approvers")
            approvers = [int(x) for x in approvers if x] if isinstance(approvers, list) else None

            rows.append({
                "odoo_id": int(r["id"]),
                "name": r.get("name"),
                "state": r.get("state"),

                # Fechas
                "date_order": parse_odoo_dt(r.get("date_order")),
                "date_approve": parse_odoo_dt(r.get("date_approve")),
                "date_planned": parse_odoo_dt(r.get("date_planned")),
                "effective_date": parse_odoo_dt(r.get("effective_date")),
                "real_reception_date": parse_odoo_date(r.get("real_reception_date")),  # date
                "fecha_pago_esperada": parse_odoo_date(r.get("x_studio_fecha_de_pago_esperada")),  # date

                # Proveedor / montos
                "partner_id": partner_id,
                "partner_name": partner_name,
                "partner_ref": r.get("partner_ref") or None,
                "amount_total": r.get("amount_total"),
                "amount_untaxed": r.get("amount_untaxed"),
                "amount_tax": r.get("amount_tax"),

                # Estados
                "invoice_status": r.get("invoice_status") or None,
                "receipt_status": r.get("receipt_status") or None,
                "x_studio_estado_de_pago": r.get("x_studio_estado_de_pago") or None,
                "x_studio_credito": bool(r.get("x_studio_credito")),

                # Comercial / contable
                "currency_id": currency_id,
                "currency_name": currency_name,
                "company_id": company_id,
                "company_name": company_name,
                "user_id": user_id,
                "user_name": user_name,
                "origin": r.get("origin"),
                "payment_term_id": payterm_id,
                "payment_term_name": payterm_name,

                # Ruta de aprobación
                "use_approval_route": r.get("use_approval_route") or None,  # selection (text)
                "is_under_approval": r.get("is_under_approval"),
                "is_fully_approved": r.get("is_fully_approved"),
                "is_approval_received": r.get("is_approval_received"),
                "approval_route_id": route_id,
                "approval_route_name": route_name,
                "current_approval_stage_id": cur_stage_id,
                "current_approval_stage_name": cur_stage_name,
                "next_approval_stage_id": nxt_stage_id,
                "next_approval_stage_name": nxt_stage_name,
                "current_approver_ids": approvers,  # jsonb

                "write_date": parse_odoo_dt(r.get("write_date")),

                # Reconciliación de borrados: si Odoo devuelve la OC, existe.
                # last_seen_at habilita rpc_mark_deleted_purchase_orders tras el FULL;
                # odoo_deleted=False "resucita" tombstones marcados por error.
                "last_seen_at": run_ts_iso or now_utc_iso(),
                "odoo_deleted": False,
                "odoo_deleted_at": None,
            })

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    modo = "FULL" if full else "incremental"
    print(f"✅ purchase_orders {modo}: {total} filas (desde write_date>{last if not full else 'TODO'})")
    return total

def mark_deleted_purchase_orders(run_ts_iso: str) -> int:
    """
    Marca odoo_deleted=true en purchase_orders cuyo last_seen_at quedó
    anterior al run del FULL resync → la OC fue eliminada (unlink) en Odoo.

    SOLO llamar inmediatamente después de sync_purchase_orders_incremental
    con full=True y el MISMO run_ts_iso. La RPC aborta con excepción si el
    volumen a marcar supera el umbral de seguridad (max(50, 5% de vivas)),
    protegiendo contra fetch parciales o resyncs incompletos.
    """
    res = sb.rpc("rpc_mark_deleted_purchase_orders", {"run_ts": run_ts_iso}).execute()
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"rpc_mark_deleted_purchase_orders error: {err}")
    data = getattr(res, "data", None)
    n = int(data) if data is not None else 0
    if n:
        print(f"🪦 purchase_orders reconciliación: {n} OC marcadas como eliminadas en Odoo")
    else:
        print("✅ purchase_orders reconciliación: sin OC eliminadas")
    return n

def backfill_purchase_orders_estado_pago(
    odoo: OdooClient,
    chunk_ids: int = 300,
    limit_ids: int = 50000,
) -> int:
    # 1) IDs donde el campo es NULL en Supabase
    res = (
        sb.table(TB_PURCHASE_ORDERS)
          .select("odoo_id")
          .is_("x_studio_estado_de_pago", "null")
          .limit(limit_ids)
          .execute()
    )
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"Supabase error leyendo purchase_orders null estado_pago: {err}")

    ids = uniq_ints([r["odoo_id"] for r in (getattr(res, "data", None) or [])])
    if not ids:
        print("✅ purchase_orders backfill estado_pago: nada que actualizar")
        return 0

    desired = ["id", "write_date", "x_studio_estado_de_pago"]
    fields = available_fields(odoo, "purchase.order", desired)

    updated = 0
    for part in chunked(ids, chunk_ids):
        batch = odoo.search_read(
            "purchase.order",
            [["id", "in", part]],
            fields,
            limit=100000,
            offset=0,
            order="id asc",
            context={"active_test": False},
        ) or []

        rows = []
        for r in batch:
            rows.append({
                "odoo_id": int(r["id"]),
                "write_date": parse_odoo_dt(r.get("write_date")),
                "x_studio_estado_de_pago": r.get("x_studio_estado_de_pago") or None,
            })

        if rows:
            sb_upsert_basic(TB_PURCHASE_ORDERS, rows, on_conflict="odoo_id", batch_size=1000)
            updated += len(rows)

    print(f"✅ purchase_orders backfill estado_pago: {updated} filas sobre {len(ids)} candidatos")
    return updated
# =========================
# Purchase Order Line Incremental
# =========================

def sync_purchase_order_lines_incremental(odoo: OdooClient, chunk: int = 1200) -> int:
    model = "purchase.order.line"
    table = TB_PURCHASE_ORDER_LINES

    desired = [
        "id","order_id","state",
        "product_id","name","product_qty","qty_received",
        "price_unit","price_subtotal","price_total",
        "date_planned",
        "currency_id","company_id",
        "analytic_distribution",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context={"active_test": False}):
        rows = []
        for r in batch:
            order_id, order_name = m2o(r.get("order_id"))
            product_id, product_name = m2o(r.get("product_id"))
            currency_id, currency_name = m2o(r.get("currency_id"))
            company_id, company_name = m2o(r.get("company_id"))

            ad = r.get("analytic_distribution")
            if ad is False:
                ad = None

            rows.append({
                "odoo_id": int(r["id"]),
                "order_id": order_id,
                "order_name": order_name,
                "state": r.get("state"),
                "product_id": product_id,
                "product_name": product_name,
                "line_name": r.get("name"),
                "product_qty": r.get("product_qty"),
                "qty_received": r.get("qty_received"),
                "price_unit": r.get("price_unit"),
                "price_subtotal": r.get("price_subtotal"),
                "price_total": r.get("price_total"),
                "date_planned": parse_odoo_dt(r.get("date_planned")),
                "currency_id": currency_id,
                "currency_name": currency_name,
                "company_id": company_id,
                "company_name": company_name,
                "analytic_distribution": ad,   # ✅ JSON
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ purchase_order_lines incremental: {total} filas (desde write_date>{last})")
    return total


# =========================
# Soft-delete (OPTIONAL / OFF by default)
# =========================
def soft_delete_stock_tables(run_ts_iso: str) -> None:
    if not ENABLE_SOFT_DELETE:
        print("ℹ️ Soft-delete OFF (recomendado para incremental).")
        return
    run_ts = dtp.parse(run_ts_iso)
    for table in (TB_PICKINGS, TB_MOVES, TB_MOVE_LINES):
        res = sb.rpc("rpc_mark_missing_stock_tables", {"table_name": table, "run_ts": run_ts.isoformat(), "days": SOFT_DELETE_DAYS}).execute()
        err = getattr(res, "error", None)
        if err:
            raise RuntimeError(f"rpc_mark_missing_stock_tables error ({table}): {err}")
        print(f"✅ Soft-delete {table}: affected={getattr(res,'data',None)}")

# =========================
# NEW: Accounting - Partners
# =========================
def sync_res_partners_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "res.partner"
    table = TB_RES_PARTNERS
 
    desired = [
        "id", "name", "vat", "is_company", "active", "write_date",
        # Contacto
        "email", "phone", "mobile",
        # Dirección
        "street", "street2", "zip", "city", "country_id",
        # Comercial
        "user_id",                    # vendedor
        "followup_responsible_id",    # cobrador
        "credit_limit",
    ]
    fields = available_fields(odoo, model, desired)
 
    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])
 
    total = 0
    ctx = {"active_test": False}
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            cid, _country_name = m2o(r.get("country_id"))
            uid, uname         = m2o(r.get("user_id"))
            fid, fname         = m2o(r.get("followup_responsible_id"))
 
            rows.append({
                "odoo_id": int(r["id"]),
                "name": (r.get("name") or "").strip() or None,
                "vat": (r.get("vat") or "").strip() or None,
                "is_company": r.get("is_company"),
                "active": r.get("active"),
 
                # Contacto
                "email": r.get("email") or None,
                "phone": r.get("phone") or None,
                "mobile": r.get("mobile") or None,
 
                # Dirección
                "street": r.get("street") or None,
                "street2": r.get("street2") or None,
                "zip": r.get("zip") or None,
                "city": r.get("city") or None,            # comuna en Odoo Chile
                "country_id": cid,
 
                # Comercial
                "user_id": uid,
                "user_name": uname,
                "followup_responsible_id": fid,
                "followup_responsible_name": fname,
                "credit_limit": r.get("credit_limit") if r.get("credit_limit") not in (False, None) else None,
 
                "write_date": parse_odoo_dt(r.get("write_date")),
            })
        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)
 
    print(f"✅ res_partners incremental: {total} filas (desde write_date>{last})")
    return total


# =========================
# NEW: Accounting - Taxes & Tax Groups
# =========================
def sync_account_tax_groups_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "account.tax.group"
    table = TB_ACCOUNT_TAX_GROUPS

    desired = ["id", "name", "write_date"]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context={"active_test": False}):
        rows: List[dict] = []
        for r in batch:
            rows.append({
                "odoo_id": int(r["id"]),
                "name": (r.get("name") or "").strip() or None,
                "write_date": parse_odoo_dt(r.get("write_date")),
            })
        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ account_tax_groups incremental: {total} filas (desde write_date>{last})")
    return total

def sync_account_taxes_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "account.tax"
    table = TB_ACCOUNT_TAXES

    desired = ["id","name","amount","amount_type","type_tax_use","tax_group_id","company_id","active","write_date"]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    ctx = {"active_test": False}
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            tg_id, tg_name = m2o(r.get("tax_group_id"))
            company_id, company_name = m2o(r.get("company_id"))
            rows.append({
                "odoo_id": int(r["id"]),
                "name": (r.get("name") or "").strip() or None,
                "amount": r.get("amount"),
                "amount_type": r.get("amount_type"),
                "type_tax_use": r.get("type_tax_use"),
                "tax_group_id": tg_id,
                "tax_group_name": tg_name,
                "company_id": company_id,
                "company_name": company_name,
                "active": r.get("active"),
                "write_date": parse_odoo_dt(r.get("write_date")),
            })
        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ account_taxes incremental: {total} filas (desde write_date>{last})")
    return total

# =========================
# NEW: Accounting - Accounts & Journals
# =========================
def sync_account_accounts_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "account.account"
    table = TB_ACCOUNT_ACCOUNTS

    desired = ["id","code","name","account_type","reconcile","deprecated","company_id","write_date"]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    ctx = {"active_test": False}
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            company_id, company_name = m2o(r.get("company_id"))
            rows.append({
                "odoo_id": int(r["id"]),
                "code": (r.get("code") or "").strip() or None,
                "name": (r.get("name") or "").strip() or None,
                "account_type": r.get("account_type"),
                "reconcile": r.get("reconcile"),
                "deprecated": r.get("deprecated"),
                "company_id": company_id,
                "company_name": company_name,
                "write_date": parse_odoo_dt(r.get("write_date")),
            })
        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ account_accounts incremental: {total} filas (desde write_date>{last})")
    return total

def sync_account_journals_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "account.journal"
    table = TB_ACCOUNT_JOURNALS

    desired = ["id","code","name","type","company_id","active","write_date"]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    ctx = {"active_test": False}
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            company_id, company_name = m2o(r.get("company_id"))
            rows.append({
                "odoo_id": int(r["id"]),
                "code": (r.get("code") or "").strip() or None,
                "name": (r.get("name") or "").strip() or None,
                "type": r.get("type"),
                "company_id": company_id,
                "company_name": company_name,
                "active": r.get("active"),
                "write_date": parse_odoo_dt(r.get("write_date")),
            })
        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ account_journals incremental: {total} filas (desde write_date>{last})")
    return total

# =========================
# NEW: Accounting - Account Moves (headers)
# =========================
def sync_account_moves_incremental(odoo: OdooClient, chunk: int = 800) -> int:
    model = "account.move"
    table = TB_ACCOUNT_MOVES

    desired = [
        "id","name","ref",
        "move_type","state",
        "invoice_date","date","invoice_date_due",
        "partner_id","journal_id","company_id",
        "currency_id",
        "amount_untaxed","amount_tax","amount_total",
        "payment_state",
        "invoice_origin",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    ctx = {"active_test": False}
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            partner_id, partner_name = m2o(r.get("partner_id"))
            journal_id, journal_name = m2o(r.get("journal_id"))
            company_id, company_name = m2o(r.get("company_id"))
            currency_id, currency_name = m2o(r.get("currency_id"))

            row = {
                "odoo_id": int(r["id"]),
                "name": r.get("name"),
                "ref": r.get("ref"),
                "move_type": r.get("move_type"),
                "state": r.get("state"),
                "invoice_date": parse_odoo_date(r.get("invoice_date")),
                "date": parse_odoo_date(r.get("date")),
                "invoice_date_due": parse_odoo_date(r.get("invoice_date_due")),
                "partner_id": partner_id,
                "partner_name": partner_name,
                "journal_id": journal_id,
                "journal_name": journal_name,
                "company_id": company_id,
                "company_name": company_name,
                "currency_id": currency_id,
                "currency_name": currency_name,
                "amount_untaxed": r.get("amount_untaxed"),
                "amount_tax": r.get("amount_tax"),
                "amount_total": r.get("amount_total"),
                "payment_state": r.get("payment_state"),
                "invoice_origin": r.get("invoice_origin"),
                "write_date": parse_odoo_dt(r.get("write_date")),
            }

            rows.append(row)

        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ account_moves incremental: {total} filas (desde write_date>{last})")
    return total

# =========================
# NEW: Accounting - Account Move Lines (heavy) via your RPC
# You said you created rpc_upsert_account_move_lines already.
# =========================
def sync_account_move_lines_incremental(odoo: OdooClient, run_ts_iso: str, chunk: int = 1500) -> int:
    model = "account.move.line"
    table = TB_ACCOUNT_MOVE_LINES
 
    desired = [
        "id",
        "move_id",
        "name",
        "account_id",
        "partner_id",
        "company_id",
        "journal_id",
        "debit",
        "credit",
        "balance",
        "amount_currency",
        "currency_id",
        "date",
        "date_maturity",
        "product_id",
        "quantity",
        "price_unit",
        "display_type",
        "analytic_distribution",
        "tax_ids",
        "tax_line_id",
        "reconciled",
        "full_reconcile_id",
        "amount_residual",
        "amount_residual_currency",
        # NUEVOS — respaldo para calcular residual cuando Odoo devuelve null
        "matched_debit_ids",
        "matched_credit_ids",
        "write_date",
    ]
    fields = available_fields(odoo, model, desired)
 
    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])
 
    acct_map: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    try:
        acct_fields = available_fields(odoo, "account.account", ["id", "code", "name"])
        acct_rows = odoo.search_read(
            "account.account",
            [],
            acct_fields,
            limit=100000,
            offset=0,
            order="id asc",
            context={"active_test": False},
        ) or []
        for a in acct_rows:
            acct_map[int(a["id"])] = ((a.get("code") or None), (a.get("name") or None))
    except Exception:
        acct_map = {}
 
    total_rows = 0
    affected = 0
    # Forzamos check_move_validity=False para que Odoo compute amount_residual al leer
    ctx = {"active_test": False, "check_move_validity": False}
 
    def _coerce_num(v):
        # Odoo devuelve False para numéricos vacíos. En Postgres queremos null.
        if v is False or v is None:
            return None
        return v
 
    def _coerce_bool(v):
        if v is None:
            return None
        if v is False or v is True:
            return v
        return None
 
    def _coerce_id_list(v):
        # Para arrays de bigint en Postgres (matched_*_ids).
        if v is False or v is None:
            return None
        if isinstance(v, list):
            return [int(x) for x in v if x not in (False, None)]
        return None
 
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
 
        for r in batch:
            aml_id = int(r["id"])
 
            move_id, _move_name = m2o(r.get("move_id"))
            account_id, _acc_name = m2o(r.get("account_id"))
            partner_id, _partner_name = m2o(r.get("partner_id"))
            company_id, company_name = m2o(r.get("company_id"))
            journal_id, journal_name = m2o(r.get("journal_id"))
            currency_id, _currency_name = m2o(r.get("currency_id"))
            product_id, _product_name = m2o(r.get("product_id"))
            tax_line_id, _tax_line_name = m2o(r.get("tax_line_id"))
            full_reconcile_id, _full_reconcile_name = m2o(r.get("full_reconcile_id"))
 
            account_code = None
            account_name = None
            if account_id and int(account_id) in acct_map:
                account_code, account_name = acct_map[int(account_id)]
 
            ad = r.get("analytic_distribution")
            if ad is False:
                ad = None
 
            # tax_ids → lista de Python (jsonb), igual que en la RPC original
            tax_ids = r.get("tax_ids")
            if tax_ids is False:
                tax_ids = None
            if isinstance(tax_ids, list):
                tax_ids = [int(x) for x in tax_ids if x]
 
            # matched_*_ids → lista de int (van a bigint[] vía RPC)
            matched_debit_ids = _coerce_id_list(r.get("matched_debit_ids"))
            matched_credit_ids = _coerce_id_list(r.get("matched_credit_ids"))
 
            row = {
                "odoo_id": aml_id,
                "move_id": move_id,
                "name": r.get("name") or None,
 
                "account_id": account_id,
                "account_code": account_code,
                "account_name": account_name,
 
                "partner_id": partner_id,
 
                "company_id": company_id,
                "company_name": company_name,
 
                "journal_id": journal_id,
                "journal_name": journal_name,
 
                "debit": _coerce_num(r.get("debit")),
                "credit": _coerce_num(r.get("credit")),
                "balance": _coerce_num(r.get("balance")),
 
                "amount_currency": _coerce_num(r.get("amount_currency")),
                "currency_id": currency_id,
 
                "date": parse_odoo_date(r.get("date")),
                "date_maturity": parse_odoo_date(r.get("date_maturity")),
 
                "product_id": product_id,
                "quantity": _coerce_num(r.get("quantity")),
                "price_unit": _coerce_num(r.get("price_unit")),
 
                "display_type": r.get("display_type") or None,
                "analytic_distribution": ad,
 
                "tax_line_id": tax_line_id,
                "tax_ids": tax_ids,   # ← jsonb-friendly
 
                "reconciled": _coerce_bool(r.get("reconciled")),
                "full_reconcile_id": full_reconcile_id,
                "amount_residual": _coerce_num(r.get("amount_residual")),
                "amount_residual_currency": _coerce_num(r.get("amount_residual_currency")),
 
                # NUEVOS
                "matched_debit_ids": matched_debit_ids,
                "matched_credit_ids": matched_credit_ids,
 
                "write_date": parse_odoo_dt(r.get("write_date")),
 
                "last_seen_at": run_ts_iso,
                "is_active": True,
                "missing_since": None,
            }
 
            row["row_hash"] = make_hash(row, [
                "odoo_id",
                "move_id",
                "name",
                "account_id",
                "account_code",
                "account_name",
                "partner_id",
                "company_id",
                "company_name",
                "journal_id",
                "journal_name",
                "debit",
                "credit",
                "balance",
                "amount_currency",
                "currency_id",
                "date",
                "date_maturity",
                "product_id",
                "quantity",
                "price_unit",
                "display_type",
                "analytic_distribution",
                "tax_line_id",
                "tax_ids",
                "reconciled",
                "full_reconcile_id",
                "amount_residual",
                "amount_residual_currency",
                "matched_debit_ids",
                "matched_credit_ids",
                "write_date",
                "is_active",
                "missing_since",
            ])
 
            rows.append(row)
 
        total_rows += len(rows)
        affected += sb_rpc_upsert("rpc_upsert_account_move_lines", rows, batch_size=BATCH_AML)
 
    print(f"✅ account_move_lines incremental: fetched={total_rows} | db_affected={affected} (desde write_date>{last})")
    return total_rows

# =========================
# NEW: Accounting - Partial Reconcile (for Aging)
# =========================
def sync_account_partial_reconciles_incremental(odoo: OdooClient, chunk: int = 1200) -> int:
    model = "account.partial.reconcile"
    table = TB_PARTIAL_RECONCILES

    desired = ["id","debit_move_id","credit_move_id","amount","max_date","create_date","write_date"]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last:
        domain.append(["write_date", ">", last])

    total = 0
    ctx = {"active_test": False}
    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            dmid, _ = m2o(r.get("debit_move_id"))
            cmid, _ = m2o(r.get("credit_move_id"))
            rows.append({
                "odoo_id": int(r["id"]),
                "debit_move_id": dmid,
                "credit_move_id": cmid,
                "amount": r.get("amount"),
                "max_date": parse_odoo_date(r.get("max_date")),
                "create_date": parse_odoo_dt(r.get("create_date")),
                "write_date": parse_odoo_dt(r.get("write_date")),
            })
        sb_upsert_basic(table, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ account_partial_reconciles incremental: {total} filas (desde write_date>{last})")
    return total


def seconds_until_next_window(now_local: datetime) -> int:
    """Si estás fuera de ventana, calcula segundos hasta el próximo inicio (WORK_START_HOUR)."""
    today_start = now_local.replace(hour=WORK_START_HOUR, minute=0, second=0, microsecond=0)
    today_end   = now_local.replace(hour=WORK_END_HOUR,   minute=0, second=0, microsecond=0)

    if now_local < today_start:
        return int((today_start - now_local).total_seconds())

    # si ya pasó el cierre, dormir hasta mañana a las 07:00
    if now_local >= today_end:
        tomorrow_start = today_start + timedelta(days=1)
        return int((tomorrow_start - now_local).total_seconds())

    return 0  # estás dentro de horario

def in_work_window(now_local: datetime) -> bool:
    start = now_local.replace(hour=WORK_START_HOUR, minute=0, second=0, microsecond=0)
    end   = now_local.replace(hour=WORK_END_HOUR,   minute=0, second=0, microsecond=0)
    return start <= now_local < end

def backfill_missing_account_moves_rpc(odoo: OdooClient, limit_ids: int = 20000, chunk_ids: int = 300) -> int:
    """
    Backfill de account.move (headers) faltantes detectados en Supabase vía RPC:
    rpc_missing_account_move_ids(limit_ids)
    """
    # 1) pedir missing ids a Supabase (DISTINCT + LEFT JOIN en SQL)
    res = sb.rpc("rpc_missing_account_move_ids", {"limit_ids": limit_ids}).execute()
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"Supabase RPC rpc_missing_account_move_ids error: {err}")

    data = getattr(res, "data", None) or []
    missing_ids = uniq_ints([r.get("move_id") for r in data if r.get("move_id") is not None])

    if not missing_ids:
        print("✅ backfill account_moves: no hay headers faltantes")
        return 0

    desired = [
        "id","name","ref",
        "move_type","state",
        "invoice_date","date","invoice_date_due",
        "partner_id","journal_id","company_id",
        "currency_id",
        "amount_untaxed","amount_tax","amount_total",
        "payment_state",
        "invoice_origin",
        "write_date",
    ]
    fields = available_fields(odoo, "account.move", desired)
    ctx = {"active_test": False}

    total = 0
    for part in chunked(missing_ids, chunk_ids):
        batch = odoo.search_read(
            "account.move",
            [["id", "in", part]],
            fields,
            limit=100000,
            offset=0,
            order="id asc",
            context=ctx
        ) or []

        rows = []
        for r in batch:
            partner_id, partner_name = m2o(r.get("partner_id"))
            journal_id, journal_name = m2o(r.get("journal_id"))
            company_id, company_name = m2o(r.get("company_id"))
            currency_id, currency_name = m2o(r.get("currency_id"))

            rows.append({
                "odoo_id": int(r["id"]),
                "name": r.get("name"),
                "ref": r.get("ref"),
                "move_type": r.get("move_type"),
                "state": r.get("state"),
                "invoice_date": parse_odoo_date(r.get("invoice_date")),
                "date": parse_odoo_date(r.get("date")),
                "invoice_date_due": parse_odoo_date(r.get("invoice_date_due")),
                "partner_id": partner_id,
                "partner_name": partner_name,
                "journal_id": journal_id,
                "journal_name": journal_name,
                "company_id": company_id,
                "company_name": company_name,
                "currency_id": currency_id,
                "currency_name": currency_name,
                "amount_untaxed": r.get("amount_untaxed"),
                "amount_tax": r.get("amount_tax"),
                "amount_total": r.get("amount_total"),
                "payment_state": r.get("payment_state"),
                "invoice_origin": r.get("invoice_origin"),
                "write_date": parse_odoo_dt(r.get("write_date")),
            })

        if rows:
            sb_upsert_basic(TB_ACCOUNT_MOVES, rows, on_conflict="odoo_id", batch_size=1000)
            total += len(rows)

    print(f"✅ backfill account_moves (RPC): {total} headers insertados (de {len(missing_ids)} faltantes)")
    return total

def backfill_missing_child_boms(odoo: OdooClient, limit_ids: int = 5000, max_rounds: int = 10) -> int:
    """
    Cierra el hueco de child BOMs (módulos semi-elaborados) referenciados en
    mrp_bom_lines cuyo header nunca entró al espejo.

    Causa raíz: sync_all_boms_incremental filtra write_date > watermark (estricto),
    y el watermark solo avanza. Los BOMs con write_date <= watermark al estrenar
    esa función quedaron invisibles. Esta función los recupera por id y reintenta
    hasta cerrar (un módulo nuevo puede revelar más hijos en sus líneas).
    """
    total = 0
    for i in range(1, max_rounds + 1):
        res = sb.rpc("rpc_missing_child_bom_ids", {"limit_ids": limit_ids}).execute()
        err = getattr(res, "error", None)
        if err:
            raise RuntimeError(f"rpc_missing_child_bom_ids error: {err}")
        data = getattr(res, "data", None) or []
        missing = uniq_ints([r.get("child_bom_id") for r in data if r.get("child_bom_id") is not None])
        if not missing:
            print(f"✅ backfill child BOMs: cerrado en ronda {i} (0 faltantes)")
            break
        print(f"🔄 backfill child BOMs ronda {i}: {len(missing)} headers faltantes")
        sync_bom_and_lines_by_ids(odoo, missing, chunk_ids=300)
        total += len(missing)
    else:
        print(f"⚠️ backfill child BOMs: alcanzó max_rounds={max_rounds}, puede quedar cola")
    print(f"✅ backfill child BOMs: {total} headers recuperados")
    return total    


def backfill_account_moves_until_done(
    odoo: OdooClient,
    batch_missing: int = 5000,
    chunk_ids: int = 300,
    max_rounds: int = 50,
    sleep_seconds: float = 0.5,
) -> int:
    """
    Backfill de account_moves usando rpc_missing_account_move_ids en rondas
    hasta completar o llegar a max_rounds.
    """
    total_inserted = 0
    for i in range(1, max_rounds + 1):
        inserted = backfill_missing_account_moves_rpc(
            odoo,
            limit_ids=batch_missing,
            chunk_ids=chunk_ids,
        )
        total_inserted += inserted
        print(f"🔁 backfill round {i}: inserted={inserted} | total={total_inserted}")

        if inserted == 0:
            break

        time.sleep(sleep_seconds)

    return total_inserted

def sync_all_boms_incremental(odoo: OdooClient, chunk: int = 800,
                              full: bool = False, run_ts_iso: Optional[str] = None) -> int:
    """
    Sincroniza TODOS los mrp.bom (no solo los referenciados por OFs).
    Esto captura BOMs de productos que aún no tienen Manufacturing Orders,
    típicamente semi-elaborados y productos terminados nuevos.

    full=True → trae TODO lo vivo (sin filtro write_date) y estampa last_seen_at
    para reconciliar bajas (unlink) en Odoo.
    """
    model = "mrp.bom"
    table = TB_BOM

    desired = [
        "id", "code", "active", "type",
        "product_tmpl_id", "product_id",
        "product_qty", "product_uom_id",
        "company_id", "write_date",
    ]
    fields = available_fields(odoo, model, desired)

    last = sb_get_max_write_date(table)
    domain: list = []
    if last and not full:
        domain.append(["write_date", ">", last])

    total = 0
    bom_ids_synced: List[int] = []
    ctx = {"active_test": False}  # incluir BOMs inactivos también, por consistencia

    for batch in iter_search_read_all(odoo, model, domain, fields, chunk=chunk, context=ctx):
        rows: List[dict] = []
        for r in batch:
            bom_id = int(r["id"])
            bom_ids_synced.append(bom_id)

            product_tmpl_id, product_tmpl_name = m2o(r.get("product_tmpl_id"))
            product_id, product_name = m2o(r.get("product_id"))
            uom_id, uom_name = m2o(r.get("product_uom_id"))
            company_id, company_name = m2o(r.get("company_id"))

            row = {
                "odoo_id": bom_id,
                "code": r.get("code"),
                "active": r.get("active"),
                "type": r.get("type"),
                "product_tmpl_id": product_tmpl_id,
                "product_tmpl_name": product_tmpl_name,
                "product_id": product_id,
                "product_name": product_name,
                "product_qty": r.get("product_qty"),
                "product_uom_id": uom_id,
                "product_uom_name": uom_name,
                "company_id": company_id,
                "company_name": company_name,
                "write_date": parse_odoo_dt(r.get("write_date")),
            }
            if full and run_ts_iso:
                row["last_seen_at"] = run_ts_iso
                row["is_active"] = True
                row["missing_since"] = None
            rows.append(row)

        sb_upsert_basic(TB_BOM, rows, on_conflict="odoo_id", batch_size=1000)
        total += len(rows)

    print(f"✅ mrp_boms {'FULL' if full else 'incremental'}: {total} filas (desde write_date>{last if not full else 'ALL'})")

    # Ahora sincronizar las líneas de TODOS los BOMs sincronizados
    if bom_ids_synced:
        sync_bom_lines_only(odoo, uniq_ints(bom_ids_synced), chunk_ids=300,
                            full=full, run_ts_iso=run_ts_iso)

    return total


def sync_bom_lines_only(odoo: OdooClient, bom_ids: List[int], chunk_ids: int = 300,
                        full: bool = False, run_ts_iso: Optional[str] = None) -> int:
    """Sincroniza solo las líneas de los BOMs especificados (sin tocar los headers).
    full=True → estampa last_seen_at en cada línea vigente (para reconciliar bajas)."""
    if not bom_ids:
        return 0

    line_fields_desired = [
        "id", "bom_id", "product_id", "product_qty", "product_uom_id",
        "operation_id", "child_bom_id", "write_date"
    ]
    line_fields = available_fields(odoo, "mrp.bom.line", line_fields_desired)

    line_rows: List[dict] = []
    for part in chunked(bom_ids, chunk_ids):
        domain = [["bom_id", "in", part]]
        batch = odoo.search_read("mrp.bom.line", domain, line_fields, limit=100000, offset=0) or []
        for r in batch:
            bom_id, bom_name = m2o(r.get("bom_id"))
            product_id, product_name = m2o(r.get("product_id"))
            uom_id, uom_name = m2o(r.get("product_uom_id"))
            child_bom_id, _ = m2o(r.get("child_bom_id"))
            op_id, op_name = m2o(r.get("operation_id"))

            row = {
                "odoo_id": int(r["id"]),
                "bom_id": bom_id,
                "bom_name": bom_name,
                "product_id": product_id,
                "product_name": product_name,
                "product_qty": r.get("product_qty"),
                "product_uom_id": uom_id,
                "product_uom_name": uom_name,
                "operation_id": op_id,
                "operation_name": op_name,
                "child_bom_id": child_bom_id,
                "write_date": parse_odoo_dt(r.get("write_date")),
            }
            if full and run_ts_iso:
                row["last_seen_at"] = run_ts_iso
                row["is_active"] = True
                row["missing_since"] = None
            line_rows.append(row)

    sb_upsert_basic(TB_BOM_LINES, line_rows, on_conflict="odoo_id", batch_size=1000)
    print(f"✅ mrp_bom_lines{' FULL' if full else ''}: {len(line_rows)} líneas sincronizadas")
    return len(line_rows)


def full_resync_mrp_tables(odoo: OdooClient, run_ts_iso: str) -> dict:
    """
    FULL resync de mrp_boms / mrp_bom_lines / manufacturing_orders + reconciliación
    de bajas (soft-delete). El incremental por write_date NO detecta unlinks en Odoo;
    esto trae TODO lo vivo (estampa last_seen_at) y marca is_active=false a lo que
    ya no aparece. Correr 1x/día (bloque FORCE_FULL_RESYNC).
    """
    print(f"🔄 FULL resync MRP (boms/lines/MO) + reconciliación | run_ts={run_ts_iso}")
    sync_all_boms_incremental(odoo, chunk=800, full=True, run_ts_iso=run_ts_iso)          # boms + líneas
    sync_manufacturing_orders_incremental(odoo, chunk=800, full=True, run_ts_iso=run_ts_iso)  # OFs

    out = {}
    for fn, label in [
        ("rpc_mark_deleted_mrp_boms", "mrp_boms"),
        ("rpc_mark_deleted_mrp_bom_lines", "mrp_bom_lines"),
        ("rpc_mark_deleted_manufacturing_orders", "manufacturing_orders"),
    ]:
        try:
            res = sb.rpc(fn, {"run_ts": run_ts_iso}).execute()
            n = getattr(res, "data", None)
            n = int(n) if isinstance(n, (int, float)) else 0
            out[label] = n
            print(f"🪦 {label}: {n} marcadas como baja (soft-delete)")
        except Exception as e:
            out[label] = f"error: {e}"
            print(f"⚠️ reconciliación {label}: {e}")
    return out


def reconcile_deletions(odoo: OdooClient, model: str, table: str, run_ts_iso: str, id_chunk: int = 5000) -> None:
    """Detección de bajas genérica (soft-delete) para tablas que YA sincronizan
    incrementalmente por su propio write_date (no necesitan re-fetch completo de datos):
    trae solo los IDs vivos de Odoo (liviano), estampa last_seen_at, y marca
    is_active=false las que ya no aparecen (con umbral de seguridad en la RPC)."""
    live_ids: List[int] = []
    for batch in iter_search_read_all(odoo, model, [], ["id"], chunk=2000, context={"active_test": False}):
        live_ids.extend(int(r["id"]) for r in batch)
    for part in chunked(live_ids, id_chunk):
        try:
            sb.rpc("rpc_stamp_last_seen", {"p_table": table, "p_ids": part, "run_ts": run_ts_iso}).execute()
        except Exception as e:
            print(f"⚠️ stamp {table}: {e}")
            return
    try:
        res = sb.rpc("rpc_mark_deleted_generic", {"p_table": table, "run_ts": run_ts_iso}).execute()
        n = getattr(res, "data", None)
        print(f"🪦 {table}: {n} marcadas baja (soft) | vivos_odoo={len(live_ids)}")
    except Exception as e:
        print(f"⚠️ reconcile {table}: {e}")


# =========================
# Main
# =========================
def main():
    odoo = OdooClient(ODOO_JSONRPC, ODOO_DB, ODOO_USER, ODOO_API_KEY)
    run_ts_iso = now_utc_iso()

    # Productos/quants incremental (ligeros)
    try:
        sync_product_products_incremental(odoo, chunk=800)
        backfill_missing_product_templates(odoo, chunk_ids=300)  # ✅

        sync_product_templates_incremental(odoo, chunk=800)

        backfill_product_templates_from_products(odoo, chunk_ids=300)

    except Exception as e:
        print(f"⚠️ product_products: {e}")

    try:
        sync_stock_quants_incremental(odoo, run_ts_iso, chunk=1000)
    except Exception as e:
        print(f"⚠️ stock_quants: {e}")

    # CRM/Sales incremental
    try:
        sync_crm_projects_incremental(odoo, chunk=800)
        backfill_crm_fme_efme(odoo, chunk_ids=300, limit_ids=20000)
        backfill_crm_nuevos_campos(odoo, chunk_ids=300, limit_ids=50_000)
    except Exception as e:
        print(f"⚠️ crm_projects: {e}")

    try:
        sync_cgestion_projects_incremental(odoo, chunk=500)
    except Exception as e:
        print(f"⚠️ cgestion_projects incremental: {e}")

    try:
        sync_cgestion_tasks_incremental(odoo, chunk=800)
    except Exception as e:
        print(f"⚠️ cgestion_tasks incremental: {e}")

    try:
        sync_sales_notes_incremental(odoo, chunk=800)
    except Exception as e:
        print(f"⚠️ sales_notes incremental: {e}")

    # MO incremental (no necesitamos los bom_ids, ahora sincronizamos TODOS los BOMs)
    try:
        sync_manufacturing_orders_incremental(odoo, chunk=800)
    except Exception as e:
        print(f"⚠️ MO incremental: {e}")

    # Backfill create_date para las OF existentes (idempotente: no-op cuando termina)
    try:
        backfill_manufacturing_orders_create_date(odoo, chunk_ids=300, limit_ids=50000)
    except Exception as e:
        print(f"⚠️ backfill MO create_date: {e}")

    # ✅ NUEVO: sincronizar TODOS los BOMs de Odoo, no solo los de OFs
    try:
        sync_all_boms_incremental(odoo, chunk=800)
    except Exception as e:
        print(f"⚠️ mrp_boms full incremental: {e}")

    try:
        backfill_missing_child_boms(odoo, limit_ids=5000, max_rounds=10)
    except Exception as e:
        print(f"⚠️ backfill child BOMs: {e}")

    # Batches de pickings (liviano; va antes para que batch_id ya tenga su fila)
    try:
        sync_picking_batches_incremental(odoo, run_ts_iso, chunk=500)
    except Exception as e:
        print(f"⚠️ picking_batches incremental: {e}")

    # Pesadas incremental + hash + RPC
    try:
        sync_pickings_incremental(odoo, run_ts_iso, chunk=1200)
    except Exception as e:
        print(f"⚠️ pickings incremental: {e}")

    try:
        sync_moves_incremental(odoo, run_ts_iso, chunk=1500)
    except Exception as e:
        print(f"⚠️ moves incremental: {e}")

    try:
        sync_move_lines_incremental(odoo, run_ts_iso, chunk=1500)
    except Exception as e:
        print(f"⚠️ move_lines incremental: {e}")

    # Soft-delete incremental (OFF por defecto; el real va en el full-resync diario)
    try:
        soft_delete_stock_tables(run_ts_iso)
    except Exception as e:
        print(f"⚠️ soft-delete: {e}")

    # ============================================================
    # FULL resync de tablas de stock (1x al día) + soft-delete
    # ============================================================
    # El incremental por write_date NO detecta borrados en Odoo.
    # Este bloque trae el universo completo de pickings/moves/move_lines,
    # puebla bom_line_id en TODO lo existente y marca inactivo (is_active=false)
    # lo que ya no está en Odoo (mata filas fantasma). Corre 1x al día al
    # cruzar STOCK_FULL_RESYNC_HOUR.
    try:
        now_local = datetime.now(TZ_LOCAL)
        import tempfile
        sentinel_stock = os.path.join(
            tempfile.gettempdir(),
            f"stock_full_resync_{now_local.date().isoformat()}.done"
        )
        if FORCE_FULL_RESYNC or (not SKIP_FULL_RESYNC
            and now_local.hour >= STOCK_FULL_RESYNC_HOUR
            and not os.path.exists(sentinel_stock)):
            print(f"🕖 Disparando FULL resync stock ({now_local})")
            full_resync_stock_tables(odoo, run_ts_iso)
            with open(sentinel_stock, "w") as f:
                f.write(now_local.isoformat())
        else:
            print(f"⏭️  FULL resync stock: skip "
                  f"(hora={now_local.hour}, target>={STOCK_FULL_RESYNC_HOUR}, "
                  f"sentinel={'existe' if os.path.exists(sentinel_stock) else 'no existe'})")
    except Exception as e:
        print(f"⚠️ FULL resync stock: {e}")

    # purchase_orders: incremental cada ciclo, FULL 1x al día (sentinel).
    # El full recupera el drift de effective_date / use_approval_route /
    # current_approvers (computed que no mueven write_date de la OC).
    try:
        now_local = datetime.now(TZ_LOCAL)
        import tempfile
        sentinel_po = os.path.join(
            tempfile.gettempdir(),
            f"purchase_orders_full_resync_{now_local.date().isoformat()}.done"
        )
        if FORCE_FULL_RESYNC or (not SKIP_FULL_RESYNC and now_local.hour >= PURCHASE_FULL_RESYNC_HOUR and not os.path.exists(sentinel_po)):
            print(f"🕖 Disparando FULL resync purchase_orders ({now_local})")
            sync_purchase_orders_incremental(odoo, chunk=800, full=True, run_ts_iso=run_ts_iso)
            mark_deleted_purchase_orders(run_ts_iso)
            with open(sentinel_po, "w") as f:
                f.write(now_local.isoformat())
        else:
            sync_purchase_orders_incremental(odoo, chunk=800, full=False)
    except Exception as e:
        print(f"⚠️ purchase_orders sync: {e}")

    try:
        backfill_purchase_orders_estado_pago(odoo, chunk_ids=300, limit_ids=50000)
    except Exception as e:
        print(f"⚠️ backfill purchase_orders estado_pago: {e}")

    try:
        sync_purchase_order_lines_incremental(odoo, chunk=1200)
    except Exception as e:
        print(f"⚠️ purchase_order_lines incremental: {e}")

    try:
        sync_sale_order_lines_incremental(odoo, chunk=1200)
    except Exception as e:
        print(f"⚠️ sale_order_lines incremental: {e}")

    try:
        sync_account_analytic_accounts_full(odoo, chunk=800)
    except Exception as e:
        print(f"⚠️ account_analytic_accounts full: {e}")

    # -------- NEW Accounting first (recommended) --------
    try:
        sync_res_partners_incremental(odoo, chunk=BATCH_PARTNERS)
    except Exception as e:
        print(f"⚠️ res_partners incremental: {e}")

    try:
        sync_account_tax_groups_incremental(odoo, chunk=BATCH_TAXES)
        sync_account_taxes_incremental(odoo, chunk=BATCH_TAXES)
    except Exception as e:
        print(f"⚠️ taxes incremental: {e}")

    try:
        sync_account_accounts_incremental(odoo, chunk=BATCH_ACCOUNTS)
        sync_account_journals_incremental(odoo, chunk=BATCH_JOURNALS)
    except Exception as e:
        print(f"⚠️ accounts/journals incremental: {e}")

    try:
        sync_account_moves_incremental(odoo, chunk=BATCH_MOVES_HDR)
    except Exception as e:
        print(f"⚠️ account_moves incremental: {e}")

    try:
        sync_account_move_lines_incremental(odoo, run_ts_iso, chunk=1500)
    except Exception as e:
        print(f"⚠️ account_move_lines incremental: {e}")

    # ============================================================
    # FULL resync de account_moves (1x al día)
    # ============================================================
    # El incremental por write_date NO detecta borrados en Odoo.
    # Este bloque corre 1x al día al cruzar ACCOUNT_MOVES_FULL_RESYNC_HOUR
    # y archiva en account_moves_archive antes de borrar.
    # Filosofía: Odoo es la ley. Si una fila no aparece, Supabase la borra.
    try:
        now_local = datetime.now(TZ_LOCAL)
        import tempfile
        sentinel = os.path.join(
            tempfile.gettempdir(),
            f"account_moves_full_resync_{now_local.date().isoformat()}.done"
        )

        if FORCE_FULL_RESYNC or (not SKIP_FULL_RESYNC
            and now_local.hour >= ACCOUNT_MOVES_FULL_RESYNC_HOUR
            and not os.path.exists(sentinel)):
            print(f"🕖 Disparando FULL resync account_moves ({now_local})")
            full_resync_account_moves_with_archive(
                odoo,
                run_ts_iso,
                date_from=ACCOUNT_MOVES_FULL_RESYNC_FROM,
            )
            with open(sentinel, "w") as f:
                f.write(now_local.isoformat())
        else:
            print(f"⏭️  FULL resync account_moves: skip "
                  f"(hora={now_local.hour}, target>={ACCOUNT_MOVES_FULL_RESYNC_HOUR}, "
                  f"sentinel={'existe' if os.path.exists(sentinel) else 'no existe'})")
    except Exception as e:
        print(f"⚠️ FULL resync account_moves: {e}")

    # ============================================================
    # FULL resync MRP (boms / bom_lines / OFs) + reconciliación de bajas (1x/día)
    # ============================================================
    try:
        now_local = datetime.now(TZ_LOCAL)
        import tempfile
        sentinel_mrp = os.path.join(
            tempfile.gettempdir(),
            f"mrp_full_resync_{now_local.date().isoformat()}.done"
        )
        if FORCE_FULL_RESYNC or (not SKIP_FULL_RESYNC
            and now_local.hour >= STOCK_FULL_RESYNC_HOUR
            and not os.path.exists(sentinel_mrp)):
            full_resync_mrp_tables(odoo, run_ts_iso)
            # Detección de bajas en líneas de OC/NV, cabeceras de venta y oportunidades CRM
            for _model, _table in [
                ("purchase.order.line", "purchase_order_lines"),
                ("sale.order.line",     "sale_order_lines"),
                ("sale.order",          "sales_notes"),
                ("crm.lead",            "crm_projects"),
            ]:
                reconcile_deletions(odoo, _model, _table, run_ts_iso)
            with open(sentinel_mrp, "w") as f:
                f.write(now_local.isoformat())
        else:
            print(f"⏭️  FULL resync MRP: skip "
                  f"(hora={now_local.hour}, skip={SKIP_FULL_RESYNC}, force={FORCE_FULL_RESYNC})")
    except Exception as e:
        print(f"⚠️ FULL resync MRP: {e}")

    try:
        sync_account_analytic_lines_incremental(odoo, run_ts_iso, chunk=1500)
    except Exception as e:
        print(f"⚠️ account_analytic_lines incremental: {e}")

    try:
        sync_account_partial_reconciles_incremental(odoo, chunk=BATCH_PARTIAL_REC)
    except Exception as e:
        print(f"⚠️ account_partial_reconciles incremental: {e}")

    try:
        backfill_missing_account_moves_rpc(odoo, limit_ids=20000, chunk_ids=300)
    except Exception as e:
        print(f"⚠️ backfill account_moves rpc: {e}")

    try:
        backfill_account_moves_until_done(odoo, batch_missing=5000, chunk_ids=300, max_rounds=10, sleep_seconds=0.5)
    except Exception as e:
        print(f"⚠️ backfill account_moves until done: {e}")

    try:
        sync_product_categories_incremental(odoo, chunk=800)
    except Exception as e:
        print(f"⚠️ product_categories incremental: {e}")

    try:
        sync_res_companies_incremental(odoo, chunk=300)
    except Exception as e:
        print(f"⚠️ res_companies incremental: {e}")

    try:
        sync_stock_valuation_layers_incremental(odoo, run_ts_iso, chunk=1200)
    except Exception as e:
        print(f"⚠️ stock_valuation_layers incremental: {e}")

    try:
        sync_account_full_reconciles_incremental(odoo, chunk=800)
    except Exception as e:
        print(f"⚠️ account_full_reconciles incremental: {e}")

    try:
        backfill_missing_account_moves(odoo, limit_ids=20000, chunk_ids=300)
    except Exception as e:
        print(f"⚠️ backfill account_moves: {e}")

if __name__ == "__main__":
    # Modo one-shot para GitHub Actions: una sola pasada y salir (el cron controla la cadencia).
    if os.getenv("RUN_ONCE", "0").strip() == "1":
        _now = datetime.now(TZ_LOCAL)
        if _now.weekday() >= 5 or not in_work_window(_now):
            print(f"🛌 Fuera de ventana L-V {WORK_START_HOUR:02d}:00-{WORK_END_HOUR:02d}:00 "
                  f"({_now.strftime('%Y-%m-%d %H:%M %Z')}). Nada que hacer.")
            raise SystemExit(0)
        main()
        raise SystemExit(0)

    while True:
        now_local = datetime.now(TZ_LOCAL)

        # Fuera de horario: dormir hasta el próximo inicio de ventana
        wait = seconds_until_next_window(now_local)
        if wait > 0:
            print(f"🛌 Fuera de horario ({now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}). "
                  f"Dormiré {wait}s hasta las {WORK_START_HOUR:02d}:00.")
            time.sleep(wait)
            continue

        # Dentro de horario: ejecutar ciclo normal
        start = time.time()
        try:
            main()
        except Exception as e:
            print(f"❌ main() falló: {e}")

        elapsed = time.time() - start
        sleep_for = max(0, LOOP_EVERY_SECONDS - elapsed)

        now_local = datetime.now(TZ_LOCAL)
        if in_work_window(now_local):
            end_today = now_local.replace(hour=WORK_END_HOUR, minute=0, second=0, microsecond=0)
            to_close = (end_today - now_local).total_seconds()
            if to_close > 0:
                sleep_for = min(sleep_for, to_close)

        print(f"⏳ Esperando {int(sleep_for)}s para la próxima ejecución…")
        time.sleep(sleep_for)
