"""
backfill_oc_por_proveedor.py
============================
Backfill histórico de OCs barriendo por RUT de proveedor (grupo Bertonati +
competidores). El endpoint ordenesdecompra.json?RutProveedor=X devuelve TODAS
las OCs de ese proveedor; luego se pide el detalle de cada una (fecha, montos,
CodigoLicitacion) → llena las OCs 'stub' y habilita el match con licitaciones.

Resume-friendly: salta las OCs que ya tienen detalle (raw_hash). Tope por
corrida (OC_PROV_MAX_DET) para caber en un job de GitHub Actions; correr varias
veces hasta drenar.
"""

import os, time, logging, requests
from datetime import datetime

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

from sync_oc import (
    parse_oc_detalle, _fetch_detalle, _mp_get, _sb_upsert, _sb_delete,
    _sb_headers, SB_REST, SLEEP, T_HDR, T_ITEMS,
)

os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "logs", f"backfill_oc_prov_{datetime.now().strftime('%Y%m%d')}.log"), encoding="utf-8")])
log = logging.getLogger("backfill_oc_prov")

# Grupo Bertonati + competidores: se leen de mp_competidores (maestro canonico).
# OC_BACKFILL_RUTS sigue disponible para forzar una lista puntual.
from competidores import ruts_objetivo

_env = (os.getenv("OC_BACKFILL_RUTS") or "").strip()
RUTS = ([r.strip() for r in _env.split(",") if r.strip()] if _env else ruts_objetivo())
MAX_DET = int(os.getenv("OC_PROV_MAX_DET", "3000"))   # tope de detalles por ejecución

def _ya_con_detalle(codigos):
    """Set de códigos que ya tienen raw_hash (detalle) en BD."""
    out = set()
    for i in range(0, len(codigos), 100):
        chunk = codigos[i:i+100]
        r = requests.get(f"{SB_REST}/{T_HDR}", headers=_sb_headers(),
                         params={"select": "codigo_oc,raw_hash",
                                 "codigo_oc": f"in.({','.join(chunk)})",
                                 "raw_hash": "not.is.null",
                                 "limit": str(len(chunk)+1)}, timeout=30)
        if r.ok:
            out.update(row["codigo_oc"] for row in r.json())
    return out

def main():
    log.info("=== backfill_oc_por_proveedor === %d RUTs | tope %d detalles/run", len(RUTS), MAX_DET)
    presupuesto = MAX_DET
    tot_ok = tot_err = 0

    for rut in RUTS:
        if presupuesto <= 0:
            log.info("Tope alcanzado; el resto queda para la próxima corrida.")
            break
        try:
            data = _mp_get({"RutProveedor": rut, "estado": "todos"})
            time.sleep(SLEEP)
            codigos = [str(x.get("Codigo")).strip() for x in (data.get("Listado") or []) if x.get("Codigo")]
        except Exception as e:
            log.error("RUT %s listado: %s", rut, repr(e)); continue

        if not codigos:
            log.info("RUT %s: sin OCs", rut); continue
        ya = _ya_con_detalle(codigos)
        pend = [c for c in codigos if c not in ya]
        log.info("RUT %s: %d OCs, %d con detalle, %d pendientes", rut, len(codigos), len(ya), len(pend))

        for cod in pend:
            if presupuesto <= 0: break
            try:
                oc = _fetch_detalle(cod); time.sleep(SLEEP)
                if not oc: continue
                hdr, items = parse_oc_detalle(oc)
                _sb_upsert(T_HDR, "codigo_oc", [hdr])
                if items:
                    _sb_delete(T_ITEMS, "codigo_oc", cod)
                    _sb_upsert(T_ITEMS, "codigo_oc,line_no", items)
                tot_ok += 1; presupuesto -= 1
            except Exception as e:
                log.warning("✗ %s: %s", cod, repr(e)); tot_err += 1

    log.info("=== Fin: %d OCs con detalle, %d errores | quedan por drenar en próximas corridas ===", tot_ok, tot_err)

    # Latido de frescura: sin esto nadie se entera si el barrido se cae. Es el job
    # que garantiza las OC del grupo, asi que su silencio tiene que ser detectable.
    try:
        from datetime import timezone
        _sb_upsert("data_freshness", "dataset", [{
            "dataset":      "mp_oc_proveedor",
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "rows_changed": tot_ok,
            "source":       "backfill_oc_por_proveedor.py",
        }])
    except Exception as e:
        log.warning("No pude estampar data_freshness: %s", repr(e))

if __name__ == "__main__":
    main()
