#!/usr/bin/env python3
"""
mp_resoluciones_worker.py — captura resoluciones de Mercado Público y clasifica la causal.

Por qué Playwright y no requests:
    El link de descarga (ViewAttachmentLC.aspx?enc=...) no es una URL estable. Es un
    postback de ASP.NET: el navegador hace POST al mismo .aspx con __VIEWSTATE,
    __EVENTVALIDATION y __EVENTTARGET = 'grdAttachment$ctl02$grdIbtnView', con la
    cookie de sesión activa. Un GET pelado a ese enc= devuelve 403 o la página de
    error — que es exactamente lo que pasa hoy: el scraper actual guarda el
    __EVENTTARGET en nombre_archivo y nunca ejecuta el postback, así que bytes,
    storage_path y texto_extraido quedan nulos.

    Se puede hacer con requests manteniendo la sesión y reenviando el viewstate,
    pero MP rota el formulario seguido. Playwright ejecuta el postback como
    navegador y captura el archivo vía expect_download(). Más lento, mucho más
    estable.

Uso:
    python mp_resoluciones_worker.py --limit 25
    python mp_resoluciones_worker.py --codigo 1057898-62-LP26

Env:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
Deps:
    pip install playwright supabase && playwright install chromium
    apt-get install -y poppler-utils            # pdftotext
    apt-get install -y ocrmypdf tesseract-ocr-spa   # opcional, para escaneados
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from supabase import create_client

FICHA = "https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx?idlicitacion={codigo}"
BUCKET = "mp-adjuntos"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])


# ---------------------------------------------------------------- clasificador

def norm(s: str) -> str:
    """
    minúsculas, sin tildes y con los saltos de línea colapsados.

    Lo último no es cosmético: las resoluciones vienen con el texto duro-envuelto
    a mitad de frase ("las ofertas presentadas no\\n   son convenientes"), y sin
    colapsar espacios ningún patrón que cruce el salto llega a matchear.
    """
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s)


# Orden = prioridad. La primera que matchea gana, así que lo más específico va arriba.
CAUSALES = [
    ("sin_ofertas", [
        r"no se (presentaron|recibieron) ofertas",
        r"sin ofertas (presentadas|recibidas)",
        r"no se present[oó] ninguna oferta",
    ]),
    ("inadmisible_administrativa", [
        r"no (adjunt|acompa|present)\w* (los )?(antecedentes|documentos|anexos)",
        r"falta de (antecedentes|documentaci[oó]n)",
        r"anexo n?[°º]?\s*\d+.{0,40}(no|omit|falt)",
        r"garant[ií]a de seriedad.{0,60}(no|falt|omit|vencid)",
    ]),
    ("inadmisible_economica", [
        r"oferta econ[oó]mica.{0,80}(excede|supera|no se ajusta|fuera de)",
        r"formato.{0,30}econ[oó]mic\w+.{0,40}(no|incorrect)",
    ]),
    ("inadmisible_plazo", [
        r"plazo de entrega.{0,60}(excede|superior|no cumple|mayor)",
        r"vigencia de la oferta.{0,60}(inferior|no cumple|menor)",
        r"(present|ingres)\w+ fuera de plazo",
    ]),
    ("inadmisible_tecnica", [
        r"no (son|resultan|fueron) convenientes t[eé]cnicamente",
        r"no se ajustan? a (los|las) (requerimientos|especificaciones|bases)",
        r"incumpl\w+ (las )?especificaciones t[eé]cnicas",
        r"no cumple con? (lo|los) (solicitado|requerido|exigido) t[eé]cnicamente",
        r"declar\w+ inadmisibles? (las |la )?ofertas?",   # fallback dentro de inadmisibilidad
    ]),
    ("supera_presupuesto", [
        r"(exceden?|superan?|sobrepasan?) (el )?(presupuesto|monto) disponible",
        r"disponibilidad presupuestaria.{0,60}(insuficiente|no permite)",
    ]),
    ("sin_financiamiento", [
        r"(sin|falta de|p[eé]rdida del) financiamiento",
        r"no cuenta con (los )?recursos",
    ]),
    ("error_bases", [
        r"error (de|en) (las )?bases",
        r"omisi[oó]n en (las )?bases",
        r"vicio.{0,30}(procedimiento|bases)",
    ]),
    ("reformulacion", [
        r"(reformular|relicitar|nuevo llamado|segundo llamado)",
        r"modificar (las )?(bases|especificaciones)",
    ]),
    ("no_conviene_interes", [
        r"no (fueran|son|resultan) convenientes a los intereses",
    ]),
]


def clasificar(texto: str) -> tuple[str, str | None]:
    """
    Devuelve (causal, fragmento_que_matcheó). 'por_determinar' si nada calza.

    El fragmento sale del texto normalizado, no del original: los offsets del match
    sólo son válidos ahí, y recortar el original con ellos devuelve el párrafo
    equivocado.
    """
    t = norm(texto)
    for causal, patrones in CAUSALES:
        for p in patrones:
            m = re.search(norm(p), t)
            if m:
                ini, fin = max(0, m.start() - 140), min(len(t), m.end() + 200)
                return causal, t[ini:fin].strip()
    return "por_determinar", None


ES_RESOLUCION = re.compile(
    r"(resoluci[oó]n|decreto|acta).{0,60}(desiert|inadmisib|revoc|adjudic)|"
    r"(desiert|inadmisib|revocaci[oó]n)",
    re.I,
)


# ------------------------------------------------------------------- extracción

def extraer_texto(pdf: Path) -> str:
    """pdftotext primero; si el PDF es escaneado (sin capa de texto), OCR."""
    try:
        out = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                             capture_output=True, timeout=90)
        txt = out.stdout.decode("utf-8", "replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        txt = ""

    if len(txt.strip()) >= 200:
        return re.sub(r"[ \t]+", " ", txt).strip()

    ocr = pdf.with_suffix(".ocr.pdf")
    try:
        subprocess.run(["ocrmypdf", "-l", "spa", "--skip-text", "--optimize", "0",
                        str(pdf), str(ocr)], capture_output=True, timeout=600, check=True)
        out = subprocess.run(["pdftotext", "-layout", str(ocr), "-"],
                             capture_output=True, timeout=90)
        return re.sub(r"[ \t]+", " ", out.stdout.decode("utf-8", "replace")).strip()
    except Exception as e:                      # noqa: BLE001
        print(f"      ! OCR no disponible o falló: {e}")
        return txt.strip()


# ------------------------------------------------------------------- navegación

def filas_anexos(page):
    """
    Lee la grilla de anexos.

    >>> VERIFICAR CONTRA EL DOM REAL <<<
    Estos selectores salen de la estructura que ya ves en tu tabla (los targets
    grdAttachment$ctlNN$grdIbtnView vienen de un GridView llamado grdAttachment).
    Si MP cambió el id del contenedor, ajustá acá y nada más.
    """
    filas = []
    rows = page.locator("table[id*='grdAttachment'] tr").all()
    for r in rows:
        celdas = [c.inner_text().strip() for c in r.locator("td").all()]
        if len(celdas) < 5:
            continue                       # header o fila de paginación
        btn = r.locator("input[type=image], a[id*='grdIbtnView']").first
        if btn.count() == 0:
            continue
        filas.append({
            "nombre": celdas[1],           # Anexo
            "tipo": celdas[2],             # Tipo
            "descripcion": celdas[3],      # Descripción
            "fecha_publicado": celdas[5] if len(celdas) > 5 else None,
            "target": btn.get_attribute("name") or btn.get_attribute("id"),
            "btn": btn,
        })
    return filas


def abrir_anexos(page, codigo: str) -> bool:
    page.goto(FICHA.format(codigo=codigo), wait_until="domcontentloaded", timeout=60_000)
    # La ficha carga los anexos en una pestaña/sección; el link cambia de nombre
    # según el estado de la licitación ("Anexos", "Ver anexos", "Archivos adjuntos").
    for texto in ("Anexos", "Ver Anexos", "Archivos Adjuntos", "Adjuntos"):
        link = page.get_by_role("link", name=re.compile(texto, re.I)).first
        if link.count():
            link.click()
            page.wait_for_load_state("networkidle", timeout=30_000)
            break
    return page.locator("table[id*='grdAttachment']").count() > 0


# ------------------------------------------------------------------ persistencia

def guardar(codigo: str, meta: dict, blob: bytes, texto: str):
    sha = hashlib.sha256(blob).hexdigest()
    path = f"{codigo}/{sha[:12]}-{re.sub(r'[^A-Za-z0-9._-]', '_', meta['nombre'])[:80]}"
    try:
        sb.storage.from_(BUCKET).upload(
            path, blob, {"content-type": "application/pdf", "upsert": "true"})
    except Exception as e:                      # noqa: BLE001
        print(f"      ! storage: {e}")

    sb.table("mp_licitacion_adjuntos").upsert({
        "codigo_externo": codigo,
        "nombre_archivo": meta["nombre"],
        "tipo": meta["tipo"],
        "descripcion": meta["descripcion"],
        "fecha_publicado": meta["fecha_publicado"],
        "es_bases": bool(re.search(r"bases", meta["tipo"] or "", re.I)),
        "postback_target": meta["target"],
        "bytes": len(blob),
        "sha256": sha,
        "storage_path": path,
        "mime": "application/pdf",
        "texto_extraido": texto[:200_000],
    }, on_conflict="codigo_externo,nombre_archivo").execute()   # PK real de la tabla
    return sha


def registrar_causal(codigo: str, texto: str):
    causal, fragmento = clasificar(texto)
    if causal == "por_determinar":
        print("      · causal no reconocida, queda para revisión manual")
        return
    inadmisible = causal.startswith("inadmisible_")
    sb.table("mp_no_adjudicada_motivo").upsert({
        "codigo_externo": codigo,
        "causal": causal,
        "bertonati_inadmisible": inadmisible or None,
        "fuente": "resolucion_pdf",
        "detalle": fragmento,
    }, on_conflict="codigo_externo").execute()
    print(f"      → causal: {causal}")


# -------------------------------------------------------------------- proceso

def procesar(page, codigo: str, tmp: Path) -> str:
    print(f"  {codigo}")
    if not abrir_anexos(page, codigo):
        print("      · sin grilla de anexos")
        return "sin_anexos"

    filas = filas_anexos(page)
    if not filas:
        return "sin_anexos"

    # Sólo lo que importa: resoluciones y actas. Las bases ya las tenés del otro carril,
    # y las declaraciones de conflicto de interés son ruido.
    objetivo = [f for f in filas
                if ES_RESOLUCION.search(f"{f['nombre']} {f['tipo']} {f['descripcion']}")]
    if not objetivo:
        print(f"      · {len(filas)} anexos, ninguno es resolución")
        return "listado"

    for f in objetivo:
        try:
            with page.expect_download(timeout=60_000) as dl:
                f["btn"].click()            # ← acá se ejecuta el postback real
            d = dl.value
            destino = tmp / d.suggested_filename
            d.save_as(destino)
            blob = destino.read_bytes()

            # el nombre real del archivo lo da el Content-Disposition, no la grilla
            f["nombre"] = d.suggested_filename or f["nombre"]
            texto = extraer_texto(destino)
            guardar(codigo, f, blob, texto)
            print(f"      ✓ {f['nombre']} · {len(blob):,} bytes · {len(texto):,} chars")
            if texto:
                registrar_causal(codigo, texto)
        except PWTimeout:
            print(f"      ! timeout descargando {f['nombre']}")
            return "error"
        except Exception as e:              # noqa: BLE001
            print(f"      ! {e}")
            return "error"
    return "listo"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codigo", help="procesar una sola licitación")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()

    if args.codigo:
        codigos = [args.codigo]
    else:
        r = sb.table("v_mp_resoluciones_pendientes").select("codigo").limit(args.limit).execute()
        codigos = [x["codigo"] for x in r.data]

    if not codigos:
        print("nada pendiente")
        return

    print(f"{len(codigos)} licitaciones en cola\n")
    with tempfile.TemporaryDirectory() as td, sync_playwright() as pw:
        tmp = Path(td)
        browser = pw.chromium.launch(headless=not args.headful)
        ctx = browser.new_context(user_agent=UA, accept_downloads=True,
                                  locale="es-CL", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        for c in codigos:
            try:
                estado = procesar(page, c, tmp)
            except Exception as e:          # noqa: BLE001
                print(f"      !! {e}")
                estado = "error"
            sb.table("mp_adjuntos_cola").upsert(
                {"codigo": c, "estado": estado}, on_conflict="codigo").execute()
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
