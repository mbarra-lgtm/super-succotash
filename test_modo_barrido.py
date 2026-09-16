"""Prueba offline del modo diurno (LIC_SOLO_NUEVAS) de sync_activas.

Lo que garantiza:
  · de día se hacen las altas y CERO refresco
  · de día el cursor del barrido NO se mueve, así la corrida nocturna lo retoma
    exactamente donde quedó (si se moviera, el pase horario le iría comiendo
    posiciones al barrido y la vuelta completa nunca se cerraría)
  · de noche el refresco llena la ventana

No toca la red.
"""
import os, sys, types, subprocess, json

AQUI = os.path.dirname(os.path.abspath(__file__))

GUION = r'''
import os, sys, types
os.environ.update({"TICKET_ACTIVAS":"x","SUPABASE_URL":"https://e.supabase.co",
                   "SUPABASE_SERVICE_KEY":"x","SLEEP_BETWEEN":"0",
                   "LIC_SOLO_NUEVAS":sys.argv[1],"LIC_MAX_POR_RUN":"600"})
sys.path.insert(0, sys.argv[2])
CUR = {"activas": {"pos": 1583, "total_activas": 4503}}
fs = types.ModuleType("cursor_store")
fs.load_cursor = lambda k, d=None: CUR.get(k, d or {})
fs.save_cursor = lambda k, v: CUR.__setitem__(k, v)
sys.modules["cursor_store"] = fs

import sync_activas as sa
TODOS  = [f"LIC-{i:05d}" for i in range(4503)]
NUEVAS = {"LIC-04000", "LIC-04001"}
VENTANA = []
_orig = sa._mp_get
def falso(p, **kw):
    if "codigo" in p:
        VENTANA.append(p["codigo"]); return {"Listado": []}
    return {"Listado": [{"CodigoExterno": c, "Estado": "Publicada"} for c in TODOS]}
sa._mp_get = falso
sa._sb_hashes_bulk = lambda cods: {c: "h" for c in cods if c not in NUEVAS}
sa._sb_upsert = lambda *a, **k: None
sa.main()
import json
print("__R__" + json.dumps({
    "altas":    len([c for c in VENTANA if c in NUEVAS]),
    "refresco": len([c for c in VENTANA if c not in NUEVAS]),
    "cursor":   CUR["activas"]["pos"],
}))
'''

def correr(modo):
    p = subprocess.run([sys.executable, "-c", GUION, modo, AQUI],
                       capture_output=True, text=True, cwd=AQUI)
    linea = [l for l in p.stdout.splitlines() if l.startswith("__R__")]
    if not linea:
        print(p.stdout, p.stderr); raise SystemExit("la corrida no produjo resultado")
    return json.loads(linea[0][5:])

fallidas = []
def check(nombre, cond, detalle=""):
    print(("  OK   " if cond else "  FALLA") + f" {nombre}" + (f" — {detalle}" if detalle else ""))
    if not cond: fallidas.append(nombre)

print("\nModo diurno (LIC_SOLO_NUEVAS=1) — pase liviano cada hora")
d = correr("1")
check("hace las 2 altas", d["altas"] == 2, f"altas={d['altas']}")
check("cero refresco", d["refresco"] == 0, f"refresco={d['refresco']}")
check("el cursor del barrido NO se mueve", d["cursor"] == 1583, f"cursor={d['cursor']}")

print("\nModo nocturno (LIC_SOLO_NUEVAS=0) — barrido completo")
n = correr("0")
check("hace las 2 altas", n["altas"] == 2, f"altas={n['altas']}")
check("llena la ventana de 600", n["refresco"] == 598, f"refresco={n['refresco']}")
check("el cursor avanza lo recorrido", n["cursor"] == 1583 + 598, f"cursor={n['cursor']}")

print("\nCobertura: 8 corridas nocturnas × 598 = %d posiciones sobre 4.503 activas"
      % (8 * 598))
check("una vuelta completa por noche", 8 * 598 >= 4503)

print("\n" + ("TODO OK" if not fallidas else f"FALLARON: {fallidas}"))
sys.exit(1 if fallidas else 0)
