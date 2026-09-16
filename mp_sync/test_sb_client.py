"""Prueba offline de sb_client. No toca la red: sustituye la sesión HTTP.

    python test_sb_client.py
"""
import os, sys, json

os.environ.setdefault("SUPABASE_URL", "https://ejemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "clave-de-prueba")
os.environ["SB_INTENTOS"] = "3"
os.environ["SB_BACKOFF"]  = "0.01"      # que la prueba no tarde

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sb_client as sb

import requests


class RespuestaFalsa:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload if payload is not None else []
        self.text = json.dumps(self._payload)
        self.content = self.text.encode()
    def json(self):
        return self._payload


LLAMADAS = []

def sesion_que_responde(*respuestas):
    """Cada llamada consume una respuesta; la última se repite si se agotan.

    Un elemento puede ser una RespuestaFalsa o una excepción a levantar.
    """
    secuencia = list(respuestas)
    LLAMADAS.clear()
    def falsa(metodo, url, **kw):
        LLAMADAS.append((metodo, url, kw))
        r = secuencia[min(len(LLAMADAS) - 1, len(secuencia) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    sb._sesion.request = falsa


fallidas = []
def check(nombre, cond, detalle=""):
    print(("  OK   " if cond else "  FALLA") + f" {nombre}" + (f" — {detalle}" if detalle else ""))
    if not cond:
        fallidas.append(nombre)

def limpiar():
    sb._fallos.clear()
    sb._seguidos = 0
    LLAMADAS.clear()


print("\n1. Un 522 transitorio se reintenta y termina bien")
limpiar()
sesion_que_responde(RespuestaFalsa(522), RespuestaFalsa(200, [{"id": 1}]))
n = sb.upsert("mp_licitaciones", "codigo_externo", [{"codigo_externo": "A"}])
check("escribió la fila", n == 1, f"n={n}")
check("hizo 2 intentos", len(LLAMADAS) == 2, f"{len(LLAMADAS)} llamadas")
check("no quedó marcado como fallo", not sb.hubo_fallos())

print("\n2. Un 522 persistente levanta y queda contabilizado")
limpiar()
sesion_que_responde(RespuestaFalsa(522))
try:
    sb.upsert("mp_oc_items", "codigo_oc,line_no", [{"codigo_oc": "X"}])
    check("levanta SupabaseError", False, "no levantó")
except sb.SupabaseError as e:
    check("levanta SupabaseError", True)
    check("agotó los 3 intentos", len(LLAMADAS) == 3, f"{len(LLAMADAS)} llamadas")
    check("registró el fallo", len(sb.fallos()) == 1, f"{len(sb.fallos())} fallos")

print("\n3. Un 400 no se reintenta (payload malo no mejora esperando)")
limpiar()
sesion_que_responde(RespuestaFalsa(400, {"message": "columna inexistente"}))
try:
    sb.upsert("mp_licitaciones", "codigo_externo", [{"ojo": 1}])
    check("levanta de inmediato", False, "no levantó")
except sb.SupabaseError:
    check("levanta de inmediato", True)
    check("un solo intento", len(LLAMADAS) == 1, f"{len(LLAMADAS)} llamadas")

print("\n4. Un corte de red se reintenta igual que un 5xx")
limpiar()
sesion_que_responde(requests.ConnectionError("conexión cortada"),
                    RespuestaFalsa(200, []))
n = sb.upsert("mp_licitaciones", "codigo_externo", [{"codigo_externo": "B"}])
check("se recuperó", n == 1 and len(LLAMADAS) == 2, f"n={n}, {len(LLAMADAS)} llamadas")

print("\n5. upsert trocea y cuenta filas escritas de verdad")
limpiar()
sesion_que_responde(RespuestaFalsa(200, []))
filas = [{"codigo_externo": f"C{i}"} for i in range(1200)]
n = sb.upsert("mp_licitaciones", "codigo_externo", filas, chunk=500)
check("3 POST (500+500+200)", len(LLAMADAS) == 3, f"{len(LLAMADAS)} llamadas")
check("devuelve 1200", n == 1200, f"n={n}")

print("\n6. select levanta en vez de devolver [] (base caída ≠ sin datos)")
limpiar()
sesion_que_responde(RespuestaFalsa(503))
try:
    sb.select("mp_adjuntos_cola", {"select": "*"})
    check("levanta", False, "devolvió en vez de levantar")
except sb.SupabaseError:
    check("levanta", True)

print("\n7. select_in trocea el filtro in.(...)")
limpiar()
sesion_que_responde(RespuestaFalsa(200, [{"codigo_externo": "Z"}]))
filas = sb.select_in("mp_licitaciones", "codigo_externo,raw_hash",
                     "codigo_externo", [f"L{i}" for i in range(450)], chunk=200)
check("3 GET (200+200+50)", len(LLAMADAS) == 3, f"{len(LLAMADAS)} llamadas")
check("concatena los resultados", len(filas) == 3, f"{len(filas)} filas")
urls = [kw["params"]["codigo_externo"] for _, _, kw in LLAMADAS]
check("el último trozo trae 50", urls[-1].count(",") == 49, urls[-1][:40] + "…")

print("\n8. select_in aborta entero si un trozo falla (nunca resultado parcial)")
limpiar()
sesion_que_responde(RespuestaFalsa(200, [{"codigo_externo": "Z"}]),
                    RespuestaFalsa(500), RespuestaFalsa(500), RespuestaFalsa(500))
try:
    sb.select_in("mp_licitaciones", "codigo_externo", "codigo_externo",
                 [f"L{i}" for i in range(400)], chunk=200)
    check("no devuelve parcial", False, "devolvió resultado parcial")
except sb.SupabaseError:
    check("no devuelve parcial", True)

print("\n9. data_freshness no se estampa si la corrida tuvo fallos")
limpiar()
sb.registrar_fallo("escritura perdida de prueba")
sesion_que_responde(RespuestaFalsa(200, []))
estampo = sb.stamp_freshness("mp_oc", 800, "sync_oc.py")
check("no estampa", estampo is False)
check("no hizo ningún request", len(LLAMADAS) == 0, f"{len(LLAMADAS)} llamadas")

print("\n10. data_freshness sí se estampa en una corrida limpia")
limpiar()
sesion_que_responde(RespuestaFalsa(200, []))
check("estampa", sb.stamp_freshness("mp_oc", 800, "sync_oc.py") is True)

print("\n11. exit_si_hubo_fallos pone el proceso en rojo")
limpiar()
sb.registrar_fallo("upsert perdido")
try:
    sb.exit_si_hubo_fallos()
    check("sale con código 1", False, "no salió")
except SystemExit as e:
    check("sale con código 1", e.code == 1, f"code={e.code}")

limpiar()
try:
    sb.exit_si_hubo_fallos()
    check("corrida limpia no sale", True)
except SystemExit:
    check("corrida limpia no sale", False, "salió sin haber fallos")

print("\n12. El cortacircuitos aborta aunque el script atrape Exception por item")
limpiar()
sb.MAX_FALLOS_SEGUIDOS = 3
sesion_que_responde(RespuestaFalsa(522))
procesados = 0
corto = False
try:
    # Reproduce el bucle del backfill: atrapa Exception y sigue con la siguiente.
    for i in range(50):
        try:
            sb.upsert("mp_oc_header", "codigo_oc", [{"codigo_oc": f"OC{i}"}])
        except Exception:
            pass
        procesados += 1
except SystemExit as e:
    corto = True
    check("corta con código 1", e.code == 1, f"code={e.code}")
check("cortó antes de las 50 OC", corto and procesados < 50, f"procesó {procesados}")
check("cortó al 3er fallo seguido", procesados == 2, f"procesó {procesados}")

print("\n13. Un éxito intermedio reinicia el contador de fallos seguidos")
limpiar()
sb.MAX_FALLOS_SEGUIDOS = 3
sesion_que_responde(RespuestaFalsa(522), RespuestaFalsa(522), RespuestaFalsa(200, []))
try:
    sb.upsert("mp_oc_header", "codigo_oc", [{"codigo_oc": "OK"}])   # falla 2, acierta
    check("no cortó tras un éxito", sb._seguidos == 0, f"seguidos={sb._seguidos}")
except SystemExit:
    check("no cortó tras un éxito", False, "cortó de más")
sb.MAX_FALLOS_SEGUIDOS = 10

print("\n" + ("TODO OK" if not fallidas else f"FALLARON: {fallidas}"))
sys.exit(1 if fallidas else 0)
