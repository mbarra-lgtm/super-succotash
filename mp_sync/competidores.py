"""
competidores.py
===============
Fuente unica de verdad de los RUTs objetivo: la tabla mp_competidores.

Antes cada script llevaba su propia lista literal de 8 RUTs, mientras el maestro
tenia 59 filas. Consecuencia medida (ago-2026): los items de OK Car, K&B,
Salinas y Fabres, Raptor, Autoque y Kovacs nunca se cargaban, aunque son 6 de
los 10 competidores que mas licitaciones nos ganan.
"""

import os, logging, requests

log = logging.getLogger("competidores")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SB_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
SB_REST      = f"{SUPABASE_URL}/rest/v1"

# Respaldo si la tabla no responde: el grupo, para no quedar sin nada.
FALLBACK = ["87.927.900-3", "77.712.689-K", "76.708.952-K"]


def norm_rut(v):
    """Canoniza un RUT a digitos + digito verificador en mayuscula."""
    return (v or "").replace(".", "").replace("-", "").strip().upper()


def ruts_objetivo(solo_grupo=False, incluir_inactivos=False):
    """RUTs desde mp_competidores. Formato con puntos y guion (lo que espera la API)."""
    params = {"select": "rut,es_bti,activo"}
    if solo_grupo:
        params["es_bti"] = "is.true"
    if not incluir_inactivos:
        params["or"] = "(activo.is.true,activo.is.null)"
    try:
        r = requests.get(f"{SB_REST}/mp_competidores",
                         headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"},
                         params={**params, "limit": "1000"}, timeout=30)
        r.raise_for_status()
        ruts = [row["rut"].strip() for row in r.json() if (row.get("rut") or "").strip()]
        if ruts:
            log.info("mp_competidores: %d RUTs objetivo", len(ruts))
            return ruts
        log.warning("mp_competidores vino vacia; uso el respaldo del grupo")
    except Exception as e:
        log.error("No pude leer mp_competidores (%s); uso el respaldo del grupo", repr(e))
    return list(FALLBACK)


def ruts_norm(**kw):
    return {norm_rut(r) for r in ruts_objetivo(**kw)}
