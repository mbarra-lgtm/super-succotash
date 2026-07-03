"""
backfill_oc_detalle.py
======================
Enriquece las Órdenes de Compra históricas que solo tienen cabecera mínima
(raw_hash null = nunca se les pidió el detalle). Pide el detalle por código y
puebla mp_oc_header + mp_oc_items completos.

Reutiliza el parser de sync_oc.py. Diseñado para correr de noche en lotes.
No usa cursor: cada corrida toma las siguientes N con raw_hash null
(las ya procesadas quedan con raw_hash y salen del conjunto pendiente).

Estimado: ~277k OC × ~2s = ~150h → repartir en varias noches.
"""

import os, time, logging, requests
from datetime import datetime

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

from sync_oc import (
    parse_oc_detalle, _fetch_detalle, _sb_upsert, _sb_delete,
    _sb_headers, SB_REST, SLEEP, T_HDR, T_ITEMS,
)

# La carpeta de logs debe existir ANTES de configurar el FileHandler
os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), exist_ok=True)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "logs", f"backfill_oc_{datetime.now().strftime('%Y%m%d')}.log"),
            encoding="utf-8"),
    ])
log = logging.getLogger("backfill_oc")

LOTE_SIZE = int(os.getenv("BACKFILL_OC_LOTE", "1500"))

def _get_pendientes(limit):
    r = requests.get(f"{SB_REST}/{T_HDR}", headers=_sb_headers(),
                     params={"select": "codigo_oc",
                             "raw_hash": "is.null",
                             "order": "codigo_oc.asc",
                             "limit": str(limit)}, timeout=30)
    return [row["codigo_oc"] for row in (r.json() if r.ok else [])]

def main():
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), exist_ok=True)
    log.info("=== backfill_oc_detalle === lote=%d sleep=%.1f", LOTE_SIZE, SLEEP)

    pendientes = _get_pendientes(LOTE_SIZE)
    if not pendientes:
        log.info("✅ Backfill OC completo — no quedan OC sin detalle.")
        return

    log.info("Procesando %d OC sin detalle", len(pendientes))
    ok = err = sin_det = 0
    for i, cod in enumerate(pendientes, 1):
        try:
            oc = _fetch_detalle(cod)
            time.sleep(SLEEP)
            if not oc:
                sin_det += 1
                continue
            hdr, items = parse_oc_detalle(oc)
            _sb_upsert(T_HDR, "codigo_oc", [hdr])
            if items:
                _sb_delete(T_ITEMS, "codigo_oc", cod)
                _sb_upsert(T_ITEMS, "codigo_oc,line_no", items)
            ok += 1
            if i % 100 == 0:
                log.info("  [%d/%d] último: %s", i, len(pendientes), cod)
        except Exception as e:
            log.warning("✗ %s: %s", cod, repr(e))
            err += 1

    log.info("=== Lote terminado: ok=%d sin_detalle=%d err=%d ===", ok, sin_det, err)

if __name__ == "__main__":
    main()
