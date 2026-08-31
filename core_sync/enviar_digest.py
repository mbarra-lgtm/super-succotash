"""
enviar_digest.py
================
Manda por correo (Brevo) las señales nuevas del radar a los destinatarios de
core_digest_destinatarios, con el mismo lenguaje visual del digest CRM de
AccessPoint: cabecera oscura, barra naranja, chips de estado, tarjeta de foco.

Programar: después de la ingesta, 1 vez al día.

    python enviar_digest.py                    # envía si hay algo que contar
    python enviar_digest.py --dry-run          # imprime el resumen, no envía
    python enviar_digest.py --a m.barra@bertonati.cl   # prueba a una dirección
    python enviar_digest.py --html /tmp/x.html # guarda el HTML para revisarlo
    python enviar_digest.py --forzar           # envía aunque no haya señales

Decisiones que conviene conocer:

* **No se manda un correo vacío.** Con las fuentes CORE actuales hay días sin
  ninguna señal, y un correo que llega vacío se deja de abrir en dos semanas.
  Sin señales el script sale en silencio (salvo --forzar).
* **La marca de agua es `digest_envio_log`, no la fecha.** Si el job no corre
  un martes, el miércoles sale todo lo acumulado en vez de perderse. Es la
  misma tabla que usan los otros digests del proyecto.
* **Un correo por persona**, para poder saludar por nombre y respetar el
  umbral de score de cada uno. Las señales se consultan una vez por umbral,
  no una vez por persona.
* **Las descartadas no se mandan.** `fn_core_senales_para_digest` las excluye,
  así que marcar una señal como falso positivo la saca del correo de mañana.

Variables de entorno:
  BREVO_API_KEY        obligatoria
  RADAR_DIGEST_FROM    remitente (default radar@bertonati.cl)
  RADAR_DIGEST_NOMBRE  nombre del remitente (default "Radar Bertonati")
"""

import os, sys, html, locale, logging, argparse
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

import sb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("enviar_digest")

# ── Config ──────────────────────────────────────────────────────────────────
BREVO_API   = "https://api.brevo.com/v3/smtp/email"
FROM_EMAIL  = os.getenv("RADAR_DIGEST_FROM", "radar@bertonati.cl")
FROM_NOMBRE = os.getenv("RADAR_DIGEST_NOMBRE", "Radar Bertonati")
DIGEST_KEY  = "radar_core"

# Paleta AccessPoint
TINTA     = "#16202D"   # cabecera y tarjeta de foco
NARANJA   = "#F4511E"   # acento y CTA
FONDO     = "#F4F4F5"
TEXTO     = "#1A1A1A"
GRIS      = "#6B7280"
GRIS_TENUE = "#9CA3AF"
BORDE     = "#E5E4E0"

DIAS  = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

# etapa → (título de sección, color del chip, fondo del chip)
ETAPAS = {
    "financiamiento_aprobado": ("Financiamiento aprobado — el dinero ya está", "#B3401A", "#FDEDE7"),
    "licitacion_publicada":    ("Licitación publicada",                        "#8A6D1F", "#FBF3DC"),
    "idea":                    ("En gestión — todavía sin decisión",           "#1F6F43", "#E7F4EC"),
    "adjudicada":              ("Adjudicada",                                  "#4B5563", "#EFF0F2"),
    "entregada":               ("Entregada",                                   "#4B5563", "#EFF0F2"),
    "rechazada":               ("Rechazada",                                   "#4B5563", "#EFF0F2"),
}
ORDEN_ETAPAS = ["financiamiento_aprobado", "licitacion_publicada", "idea",
                "adjudicada", "entregada", "rechazada"]

CATEGORIAS = {
    "ambulancia": "Ambulancia", "carro_bomba": "Carro bomba", "blindado": "Blindado",
    "movil_policial": "Móvil policial", "rescate": "Rescate",
    "carroceria": "Carrocería", "otro": "Otro",
}


def _exigir_brevo() -> str:
    clave = os.getenv("BREVO_API_KEY", "").strip()
    if not clave:
        log.error("Falta BREVO_API_KEY. En GitHub: Settings → Secrets and "
                  "variables → Actions → New repository secret.")
        sys.exit(2)
    return clave


# ── Formato ─────────────────────────────────────────────────────────────────

def _cl(numero: float, decimales: int = 1) -> str:
    """Formato chileno: 1.612,2 — punto de miles, coma decimal."""
    txt = f"{numero:,.{decimales}f}"
    return txt.replace(",", "@").replace(".", ",").replace("@", ".")


def _mm(valor) -> str:
    if not valor:
        return ""
    return f"${_cl(float(valor) / 1_000_000)} MM"


def _fecha_larga() -> str:
    hoy = datetime.now(timezone.utc).astimezone()
    return f"{DIAS[hoy.weekday()]}, {hoy.day} de {MESES[hoy.month-1]} de {hoy.year}"


def _chip(texto: str, color: str, fondo: str, punto: bool = True) -> str:
    bolita = (f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;'
              f'background:{color};margin-right:6px;vertical-align:middle;"></span>') if punto else ""
    return (f'<span style="display:inline-block;background:{fondo};color:{color};'
            f'font-size:12px;font-weight:600;padding:5px 11px;border-radius:20px;'
            f'margin:0 6px 6px 0;">{bolita}{texto}</span>')


def _etiqueta(texto: str) -> str:
    return (f'<div style="font-size:10px;font-weight:700;letter-spacing:.11em;'
            f'text-transform:uppercase;color:{GRIS_TENUE};margin-bottom:5px;">{texto}</div>')


# ── Piezas del correo ───────────────────────────────────────────────────────

def _cabecera() -> str:
    return f"""
    <tr><td style="background:{TINTA};padding:22px 30px 18px;border-radius:10px 10px 0 0;">
      <div style="font-size:19px;font-weight:800;letter-spacing:.16em;color:#fff;">BERTONATI</div>
      <div style="font-size:10px;font-weight:600;letter-spacing:.12em;color:#8B95A3;margin-top:5px;">
        VEHÍCULOS ESPECIALES · RADAR COMERCIAL · CORE
      </div>
    </td></tr>
    <tr><td style="background:{NARANJA};height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>"""


def _titular(nombre: str, senales: list) -> str:
    n = len(senales)
    regiones = sorted({s.get("region") for s in senales if s.get("region")})
    contexto = ", ".join(regiones[:3]) + ("…" if len(regiones) > 3 else "")

    conteo = {}
    for s in senales:
        conteo[s["etapa"]] = conteo.get(s["etapa"], 0) + 1
    chips = "".join(
        _chip(f"{conteo[e]} {ETAPAS[e][0].split(' — ')[0].lower()}", ETAPAS[e][1], ETAPAS[e][2])
        for e in ORDEN_ETAPAS if e in conteo)

    plural = "señales nuevas" if n != 1 else "señal nueva"
    return f"""
    <tr><td style="padding:26px 30px 0;">
      <div style="font-size:22px;font-weight:700;color:{TEXTO};line-height:1.3;">
        {html.escape(nombre)}, hay {n} {plural} en el radar.
      </div>
      <div style="font-size:13px;color:{GRIS};margin-top:7px;">
        {_fecha_larga()} · Consejos Regionales y prensa · {html.escape(contexto or "Chile")}
      </div>
      <div style="margin-top:14px;">{chips}</div>
    </td></tr>"""


def _cifras(senales: list) -> str:
    total  = sum(float(s["monto_clp"]) for s in senales if s.get("monto_clp"))
    con_monto = sum(1 for s in senales if s.get("monto_clp"))
    comunas = {s.get("comuna") for s in senales if s.get("comuna")}
    unidades = sum(int(s["unidades"]) for s in senales if s.get("unidades"))

    def caja(etiqueta, valor, pie):
        return f"""<td width="50%" style="padding:0 6px;">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid {BORDE};border-radius:8px;">
            <tr><td style="padding:15px 17px;">
              {_etiqueta(etiqueta)}
              <div style="font-size:21px;font-weight:700;color:{TEXTO};">{valor}</div>
              <div style="font-size:12px;color:{GRIS_TENUE};margin-top:3px;">{pie}</div>
            </td></tr>
          </table></td>"""

    return f"""
    <tr><td style="padding:20px 24px 0;">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        {caja("Monto aprobado detectado", f"${_cl(total/1_000_000)} MM",
              f"En {con_monto} de {len(senales)} señales")}
        {caja("Cobertura", f"{len(comunas) or '—'} comuna{'s' if len(comunas) != 1 else ''}",
              f"{unidades} unidad{'es' if unidades != 1 else ''} identificada{'s' if unidades != 1 else ''}" if unidades else "Sin unidades informadas")}
      </tr></table>
    </td></tr>"""


def _foco(s: dict) -> str:
    e = html.escape
    etapa_txt = ETAPAS.get(s["etapa"], ("", "", ""))[0].split(" — ")[0]
    lugar = s.get("comuna") or s.get("region") or ""

    def dato(etiqueta, valor):
        return f"""<td style="padding-right:30px;vertical-align:top;">
          <div style="font-size:10px;font-weight:700;letter-spacing:.11em;
                      text-transform:uppercase;color:#8B95A3;margin-bottom:5px;">{etiqueta}</div>
          <div style="font-size:16px;font-weight:700;color:#fff;">{valor}</div>
        </td>"""

    return f"""
    <tr><td style="padding:22px 24px 0;">
      <table width="100%" cellpadding="0" cellspacing="0"
             style="background:{TINTA};border-radius:10px;">
        <tr><td style="padding:22px 24px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:.12em;
                      text-transform:uppercase;color:{NARANJA};margin-bottom:9px;">
            Tu foco de hoy
          </div>
          <div style="font-size:17px;font-weight:700;color:#fff;line-height:1.35;">
            {e(s['titulo'])}
          </div>
          <div style="font-size:13px;color:#8B95A3;margin-top:5px;">
            {e(s.get('organismo') or s.get('fuente_nombre') or '')}{' · ' + e(lugar) if lugar else ''}
          </div>

          <table cellpadding="0" cellspacing="0" style="margin-top:18px;"><tr>
            {dato("Monto", _mm(s.get("monto_clp")) or "No informado")}
            {dato("Unidades", str(s["unidades"]) if s.get("unidades") else "—")}
            {dato("Etapa", e(etapa_txt))}
            {dato("Score", str(s["score"]))}
          </tr></table>

          {f'''<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:18px;">
            <tr><td style="background:#1F2C3D;border-radius:7px;padding:15px 17px;">
              <div style="font-size:13px;color:#D5DAE1;line-height:1.55;">{e(s["por_que_importa"])}</div>
              <div style="font-size:11px;color:#7C8797;margin-top:9px;">
                Confianza del clasificador: {int(float(s.get("confianza") or 0)*100)}% ·
                Verifica en la fuente antes de contactar
              </div>
            </td></tr></table>''' if s.get("por_que_importa") else ''}

          <a href="{e(s['fuente_url'])}"
             style="display:inline-block;margin-top:18px;background:{NARANJA};color:#fff;
                    font-size:14px;font-weight:600;text-decoration:none;
                    padding:12px 20px;border-radius:7px;">
            Abrir la fuente &rarr;
          </a>
        </td></tr>
      </table>
    </td></tr>"""


def _fila(s: dict) -> str:
    e = html.escape
    lugar = s.get("comuna") or s.get("region") or "—"
    color = ETAPAS.get(s["etapa"], ("", GRIS, "#EFF0F2"))[1]
    fondo = ETAPAS.get(s["etapa"], ("", GRIS, "#EFF0F2"))[2]

    chips = _chip(CATEGORIAS.get(s["categoria"], s["categoria"]), "#4B5563", "#EFF0F2", punto=False)
    if s.get("unidades"):
        chips += _chip(f"{s['unidades']} unidad{'es' if s['unidades'] != 1 else ''}",
                       color, fondo, punto=False)
    if s.get("fecha_evento"):
        chips += _chip(str(s["fecha_evento"]), "#4B5563", "#EFF0F2", punto=False)

    return f"""
    <tr><td style="padding:13px 0;border-bottom:1px solid {BORDE};">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="width:3px;background:{color};border-radius:2px;">&nbsp;</td>
        <td style="padding-left:14px;vertical-align:top;">
          <a href="{e(s['fuente_url'])}"
             style="font-size:14px;font-weight:600;color:{TEXTO};
                    text-decoration:none;line-height:1.35;">{e(s['titulo'])}</a>
          <div style="font-size:12px;color:{GRIS};margin:4px 0 7px;">{e(lugar)}</div>
          <div>{chips}</div>
        </td>
        <td align="right" style="vertical-align:top;white-space:nowrap;padding-left:12px;">
          <div style="font-size:15px;font-weight:700;color:{TEXTO};">{_mm(s.get('monto_clp')) or '—'}</div>
          <div style="font-size:11px;color:{GRIS_TENUE};margin-top:3px;">score {s['score']}</div>
        </td>
      </tr></table>
    </td></tr>"""


def _seccion(etapa: str, grupo: list) -> str:
    titulo, color, _ = ETAPAS.get(etapa, (etapa, GRIS, "#EFF0F2"))
    total = sum(float(s["monto_clp"]) for s in grupo if s.get("monto_clp"))
    resumen = f"{len(grupo)} señal{'es' if len(grupo) != 1 else ''}"
    if total:
        resumen += f" · ${_cl(total/1_000_000)} MM"

    filas = "".join(_fila(s) for s in grupo)
    return f"""
    <tr><td style="padding:26px 30px 0;">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="font-size:13px;font-weight:700;color:{TEXTO};">
          <span style="display:inline-block;width:7px;height:7px;border-radius:50%;
                       background:{color};margin-right:8px;"></span>{titulo}
        </td>
        <td align="right" style="font-size:12px;color:{GRIS_TENUE};">{resumen}</td>
      </tr></table>
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">{filas}</table>
    </td></tr>"""


def armar_html(nombre: str, senales: list) -> str:
    foco, resto = senales[0], senales[1:]

    por_etapa = {}
    for s in resto:
        por_etapa.setdefault(s["etapa"], []).append(s)

    secciones = "".join(
        _seccion(e, por_etapa[e]) for e in ORDEN_ETAPAS if e in por_etapa)
    secciones += "".join(
        _seccion(e, g) for e, g in por_etapa.items() if e not in ORDEN_ETAPAS)

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{FONDO};
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
 <table width="100%" cellpadding="0" cellspacing="0" style="background:{FONDO};padding:26px 12px;">
  <tr><td align="center">
   <table width="640" cellpadding="0" cellspacing="0"
          style="max-width:640px;background:#fff;border-radius:10px;overflow:hidden;">
     {_cabecera()}
     {_titular(nombre, senales)}
     {_cifras(senales)}
     {_foco(foco)}
     {secciones}
     <tr><td style="padding:28px 30px 26px;">
       <div style="border-top:1px solid {BORDE};padding-top:16px;
                   font-size:11px;color:{GRIS_TENUE};line-height:1.6;">
         Acuerdos de Consejos Regionales y prensa regional. El score combina etapa,
         tamaño, prioridad del organismo y confianza del clasificador.<br>
         El análisis de cada señal incluye inferencias del modelo: el link lleva al
         documento original. Si una señal no corresponde, responde este correo
         indicándolo — sirve para afinar el filtro.
       </div>
     </td></tr>
   </table>
  </td></tr>
 </table>
</body></html>"""


def armar_asunto(senales: list) -> str:
    top = senales[0]
    if len(senales) == 1:
        return f"Radar: {top['titulo'][:68]}"
    lugar = top.get("comuna") or top.get("region") or top["categoria"]
    return f"Radar: {len(senales)} señales nuevas · encabeza {lugar}"


# ── Envío ───────────────────────────────────────────────────────────────────

def enviar(destinatario: dict, asunto: str, cuerpo: str) -> tuple[bool, dict]:
    payload = {
        "sender": {"email": FROM_EMAIL, "name": FROM_NOMBRE},
        "to": [{"email": destinatario["email"],
                "name": destinatario.get("nombre") or destinatario["email"]}],
        "subject": asunto,
        "htmlContent": cuerpo,
    }
    r = requests.post(BREVO_API, timeout=45, json=payload, headers={
        "api-key": _exigir_brevo(), "content-type": "application/json",
        "accept": "application/json"})
    try:
        respuesta = r.json()
    except Exception:
        respuesta = {"texto": r.text[:400]}
    return r.status_code < 300, {"status": r.status_code, "respuesta": respuesta}


def registrar(ok: bool, detalle: dict, email: str, n: int) -> None:
    ahora = datetime.now(timezone.utc)
    sb.insert("digest_envio_log", [{
        "digest_key": DIGEST_KEY,
        "fecha_cl": ahora.astimezone().date().isoformat(),
        "source": "core_sync/enviar_digest.py",
        "disparado_at": ahora.isoformat(),
        "resuelto_at": ahora.isoformat(),
        "status_code": detalle.get("status"),
        "ok": ok,
        "respuesta": {**detalle.get("respuesta", {}), "para": email, "senales": n},
        "error_msg": None if ok else str(detalle.get("respuesta"))[:500],
    }])


def main() -> int:
    ap = argparse.ArgumentParser(description="Digest por correo del radar comercial")
    ap.add_argument("--dry-run", action="store_true", help="imprime, no envía")
    ap.add_argument("--a", help="enviar solo a esta dirección (prueba)")
    ap.add_argument("--html", help="guardar el HTML generado en este archivo")
    ap.add_argument("--forzar", action="store_true", help="enviar aunque no haya señales")
    args = ap.parse_args()

    if args.a:
        destinatarios = [{"email": args.a, "nombre": args.a.split("@")[0].split(".")[-1].title(),
                          "score_minimo": 45}]
    else:
        destinatarios = sb.select("core_digest_destinatarios", {
            "select": "email,nombre,score_minimo", "activo": "is.true",
            "order": "score_minimo.asc"})
        if not destinatarios:
            log.warning("No hay destinatarios activos en core_digest_destinatarios")
            return 0

    cache: dict[int, list] = {}
    hubo_error = False

    for d in destinatarios:
        umbral = d["score_minimo"]
        if umbral not in cache:
            cache[umbral] = sb.rpc("fn_core_senales_para_digest",
                                   {"p_score_minimo": umbral}) or []
        senales = cache[umbral]
        log.info("%s (umbral %d) → %d señal(es)", d["email"], umbral, len(senales))

        if not senales and not args.forzar:
            log.info("   sin señales nuevas: no se envía "
                     "(un correo vacío mata el hábito)")
            continue

        nombre = (d.get("nombre") or d["email"].split("@")[0]).split()[0]
        asunto = armar_asunto(senales)
        cuerpo = armar_html(nombre, senales)

        if args.html:
            with open(args.html, "w", encoding="utf-8") as f:
                f.write(cuerpo)
            log.info("   HTML guardado en %s", args.html)

        if args.dry_run:
            print(f"\n{'='*72}\nPARA: {d['email']}\nASUNTO: {asunto}\n{'='*72}")
            for s in senales:
                print(f"  [{s['score']:3d}] {s['etapa']:24s} {s['titulo'][:70]}")
            continue

        ok, detalle = enviar(d, asunto, cuerpo)
        registrar(ok, detalle, d["email"], len(senales))
        if ok:
            log.info("   enviado (HTTP %s)", detalle["status"])
        else:
            hubo_error = True
            log.error("   FALLÓ (HTTP %s): %s", detalle["status"], detalle["respuesta"])

    # Acá sí importa el exit code: si el correo no salió, nadie se entera solo.
    return 1 if hubo_error else 0


if __name__ == "__main__":
    sys.exit(main())
