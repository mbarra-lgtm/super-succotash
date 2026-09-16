"""Prueba offline de la lógica de ventanas troceadas de sync_compra_agil.

No toca Mercado Público ni Supabase: sustituye _mp_get y las escrituras por
dobles en memoria y verifica el comportamiento del cursor.
"""
import os, sys, types
from datetime import datetime, timedelta

os.environ.setdefault("TICKET_CA", "x")
os.environ.setdefault("SUPABASE_URL", "https://ejemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ["SLEEP_BETWEEN"] = "0"
os.environ["CA_CHUNK_HORAS"] = "6"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# cursor_store en memoria, antes de importar el módulo bajo prueba
CURSOR = {}
falso_store = types.ModuleType("cursor_store")
falso_store.load_cursor = lambda key, default=None: CURSOR.get(key, default or {})
falso_store.save_cursor = lambda key, value: CURSOR.__setitem__(key, value)
sys.modules["cursor_store"] = falso_store

import sync_compra_agil as ca

TZ = ca.TZ_CL
ESCRITAS = []

def reset(cursor_desde_horas):
    CURSOR.clear(); ESCRITAS.clear()
    inicio = datetime.now(TZ) - timedelta(hours=cursor_desde_horas)
    CURSOR["compra_agil"] = {"ultimo_cambio": ca._fmt(inicio)}
    return inicio

def instalar_dobles(fallar_en_llamada=None):
    """_mp_get devuelve 1 página con 2 items; falla en la llamada N si se pide."""
    estado = {"n": 0}
    def falso_mp_get(path, params):
        estado["n"] += 1
        if fallar_en_llamada and estado["n"] == fallar_en_llamada:
            raise ca.MPTransitorio("429 simulado")
        desde = params["cambio_desde"]
        return {"paginacion": {"total_paginas": 1},
                "items": [{"codigo": f"CA-{desde}-{i}", "estado": {"codigo": "publicada"},
                           "fechas": {}, "montos": {}, "institucion": {},
                           "convocatoria": {}, "resumen": {}, "motivos": {},
                           "nombre": "prueba"} for i in range(2)]}
    ca._mp_get = falso_mp_get
    ca._sb_upsert = lambda t, oc, rows: ESCRITAS.extend(rows)
    ca._sb_con_detalle = lambda ids: set()
    ca._sync_detalle = lambda i: None
    return estado

def horas_de_atraso():
    dt = datetime.fromisoformat(CURSOR["compra_agil"]["ultimo_cambio"]).replace(tzinfo=TZ)
    return (datetime.now(TZ) - dt).total_seconds() / 3600

fallos = []
def check(nombre, cond, detalle=""):
    print(("  OK   " if cond else "  FALLA") + f" {nombre}" + (f" — {detalle}" if detalle else ""))
    if not cond: fallos.append(nombre)

print("\n1. Atraso de 48 h drena completo en una corrida")
reset(48)
instalar_dobles()
ca.main()
check("cursor queda al día", horas_de_atraso() < 0.2, f"atraso {horas_de_atraso():.2f} h")
# 48 h / 6 h = 8 trozos completos + 1 parcial (los segundos transcurridos desde reset)
check("escribió 8-9 trozos × 2 filas", len(ESCRITAS) in (16, 18), f"{len(ESCRITAS)} filas")

print("\n2. Fallo en el 5º trozo conserva el progreso de los 4 anteriores")
inicio = reset(48)
instalar_dobles(fallar_en_llamada=5)
try:
    ca.main()
except SystemExit as e:
    check("no sale con 1 (sí hubo progreso)", e.code != 1, f"code={e.code}")
avance_h = (datetime.fromisoformat(CURSOR["compra_agil"]["ultimo_cambio"]).replace(tzinfo=TZ)
            - inicio).total_seconds() / 3600
check("cursor avanzó exactamente 4 trozos (24 h)", abs(avance_h - 24) < 0.01, f"avanzó {avance_h:.2f} h")
check("escribió solo lo de los trozos cerrados", len(ESCRITAS) == 8, f"{len(ESCRITAS)} filas")

print("\n3. Fallo en el 1er trozo: sin progreso → exit 1 (el job se pone rojo)")
inicio = reset(48)
instalar_dobles(fallar_en_llamada=1)
codigo = 0
try:
    ca.main()
except SystemExit as e:
    codigo = e.code
check("sale con código 1", codigo == 1, f"code={codigo}")
check("cursor intacto", CURSOR["compra_agil"]["ultimo_cambio"] == ca._fmt(inicio))

print("\n4. Presupuesto de tiempo corta sin perder lo ya cerrado")
inicio = reset(240)                      # 10 días = 40 trozos
ca.PRESUP_LISTADO_S = 0.05
instalar_dobles()
import time as _t
_orig = ca._procesar_ventana
def lento(d, h):
    _t.sleep(0.02)
    return _orig(d, h)
ca._procesar_ventana = lento
ca.main()
avance_h = (datetime.fromisoformat(CURSOR["compra_agil"]["ultimo_cambio"]).replace(tzinfo=TZ)
            - inicio).total_seconds() / 3600
check("avanzó algo pero no todo", 0 < avance_h < 240, f"avanzó {avance_h:.0f} h de 240")
resto = avance_h % 6                      # tolerancia por ambos lados: 5.9999 ≈ 0
check("el avance es múltiplo exacto del trozo", min(resto, 6 - resto) < 0.01, f"{avance_h:.4f} h")

print("\n" + ("TODO OK" if not fallos else f"FALLARON: {fallos}"))
sys.exit(1 if fallos else 0)
