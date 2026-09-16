"""Prueba offline de las funciones que reemplazan hijos (items / adjudicaciones).

Estas funciones emiten DELETE contra datos de producción, así que lo que se
verifica es lo que no debe pasar nunca:

  · que se borre antes de escribir (dejaría la licitación vacía si falla el write)
  · que se borre una fila que sí venía en el detalle nuevo

No toca la red: sustituye sb.upsert / sb.select / sb.delete por espías.
"""
import os, sys

for v in ("TICKET_OC", "TICKET_CRM", "TICKET_ACTIVAS", "TICKET_CA",
          "TICKET_BACKFILL", "TICKET_ESTADOS"):
    os.environ.setdefault(v, "x")
os.environ.setdefault("SUPABASE_URL", "https://ejemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sb_client as sb

EVENTOS = []          # [("upsert"|"delete"|"select", tabla, detalle)]
PREVIAS = []          # lo que devuelve sb.select

def espiar():
    EVENTOS.clear()
    sb.upsert = lambda t, oc, rows, **kw: EVENTOS.append(("upsert", t, len(rows))) or len(rows)
    sb.delete = lambda t, filtros: EVENTOS.append(("delete", t, dict(filtros)))
    sb.select = lambda t, params: (EVENTOS.append(("select", t, dict(params))), list(PREVIAS))[1]

fallidas = []
def check(nombre, cond, detalle=""):
    print(("  OK   " if cond else "  FALLA") + f" {nombre}" + (f" — {detalle}" if detalle else ""))
    if not cond: fallidas.append(nombre)

def deletes(tabla=None):
    return [e for e in EVENTOS if e[0] == "delete" and (tabla is None or e[1] == tabla)]

def orden_ok(tabla):
    """El primer upsert de la tabla ocurre antes que el primer delete."""
    idx_u = next((i for i, e in enumerate(EVENTOS) if e[0] == "upsert" and e[1] == tabla), None)
    idx_d = next((i for i, e in enumerate(EVENTOS) if e[0] == "delete" and e[1] == tabla), None)
    return idx_u is not None and (idx_d is None or idx_u < idx_d)


print("\n1. sync_oc._reemplazar_items — escribe antes de borrar, y borra por not.in")
import sync_oc
espiar(); PREVIAS[:] = []
items = [{"codigo_oc": "OC1", "line_no": n} for n in (1, 2, 3)]
sync_oc._reemplazar_items("OC1", items)
check("upsert antes del delete", orden_ok("mp_oc_items"))
d = deletes("mp_oc_items")
check("un solo delete", len(d) == 1, f"{len(d)} deletes")
check("filtra por la OC", d[0][2].get("codigo_oc") == "eq.OC1", str(d[0][2]))
check("excluye las líneas vivas", d[0][2].get("line_no") == "not.in.(1,2,3)", str(d[0][2]))

print("\n2. sync_estados_diario._reemplazar_adj — solo borra las que desaparecieron")
import sync_estados_diario as sed
espiar()
PREVIAS[:] = [{"item_no": 1, "proveedor_rut": "A"},      # sigue
              {"item_no": 2, "proveedor_rut": "B"},      # desaparece
              {"item_no": 3, "proveedor_rut": "C"}]      # desaparece
nuevas = [{"licitacion_id": "L1", "item_no": 1, "proveedor_rut": "A"},
          {"licitacion_id": "L1", "item_no": 4, "proveedor_rut": "D"}]
sed._reemplazar_adj("L1", nuevas)
check("upsert antes del delete", orden_ok(sed.T_ADJ))
d = deletes(sed.T_ADJ)
check("borra exactamente 2", len(d) == 2, f"{len(d)} deletes")
borradas = {(e[2]["item_no"], e[2]["proveedor_rut"]) for e in d}
check("borra (2,B) y (3,C)", borradas == {("eq.2", "eq.B"), ("eq.3", "eq.C")}, str(borradas))
check("NO borra la que sigue viva (1,A)", ("eq.1", "eq.A") not in borradas)

print("\n3. Caso crítico: nada cambió → no se borra nada")
espiar()
PREVIAS[:] = [{"item_no": 1, "proveedor_rut": "A"}]
sed._reemplazar_adj("L1", [{"licitacion_id": "L1", "item_no": 1, "proveedor_rut": "A"}])
check("cero deletes", len(deletes(sed.T_ADJ)) == 0, f"{len(deletes(sed.T_ADJ))} deletes")

print("\n4. Tipos mezclados (int en BD, str en el detalle) no provocan borrados falsos")
espiar()
PREVIAS[:] = [{"item_no": 7, "proveedor_rut": "76.123.456-7"}]      # int
sed._reemplazar_adj("L1", [{"licitacion_id": "L1", "item_no": "7",  # str
                            "proveedor_rut": "76.123.456-7"}])
check("no borra por diferencia de tipo", len(deletes(sed.T_ADJ)) == 0,
      str([e[2] for e in deletes(sed.T_ADJ)]))

print("\n5. sync_crm._limpiar_sobrantes — items por not.in y adj una por una")
import sync_crm
espiar()
PREVIAS[:] = [{"item_no": 1, "proveedor_rut": "A"}, {"item_no": 9, "proveedor_rut": "Z"}]
sync_crm._limpiar_sobrantes(
    "L9",
    [{"codigo_externo": "L9", "item_no": 1}, {"codigo_externo": "L9", "item_no": 2}],
    [{"codigo_externo": "L9", "item_no": 1, "proveedor_rut": "A"}])
di = deletes(sync_crm.T_ITEMS)
check("items: un delete con not.in", len(di) == 1 and di[0][2]["item_no"] == "not.in.(1,2)",
      str(di[0][2]) if di else "sin delete")
da = deletes(sync_crm.T_ADJ)
check("adj: borra solo la huérfana (9,Z)", len(da) == 1 and da[0][2]["item_no"] == "eq.9",
      str([e[2] for e in da]))

print("\n6. Sin items nuevos, sync_crm sí limpia todo (el detalle vino vacío a propósito)")
espiar(); PREVIAS[:] = []
sync_crm._limpiar_sobrantes("L9", [], [])
di = deletes(sync_crm.T_ITEMS)
check("delete sin filtro not.in", len(di) == 1 and "item_no" not in di[0][2], str(di[0][2]) if di else "—")

print("\n7. backfill_noche._reemplazar_hijos — no toca nada si el detalle vino vacío")
import backfill_noche
espiar(); PREVIAS[:] = []
backfill_noche._reemplazar_hijos("L5", [], [])
check("cero operaciones", len(EVENTOS) == 0, f"{len(EVENTOS)} eventos: {EVENTOS}")

print("\n" + ("TODO OK" if not fallidas else f"FALLARON: {fallidas}"))
sys.exit(1 if fallidas else 0)
