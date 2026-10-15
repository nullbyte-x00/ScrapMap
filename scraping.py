"""Scraper v2: Google Maps -> Postgres -> auditoría web -> mensajes por WhatsApp.

Uso:
  python scraping.py "restaurantes" "Aguachica"      # una búsqueda
  python scraping.py --lote lote.json                # varias búsquedas
  python scraping.py --seguimientos                  # follow-ups a los 3 días
  python scraping.py --listar [estado]               # ver prospectos
  python scraping.py --estado 12 contactado          # cambiar estado
  python scraping.py --stats                         # embudo y A/B

Formato de lote.json:
  {"negocios": ["restaurantes", "odontólogos"], "lugares": ["Aguachica", "Ocaña"]}
"""
import argparse
import asyncio
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse

import psycopg2
from dotenv import load_dotenv
from groq import Groq
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import auditoria
import db

load_dotenv()


# ---------------- salida y cancelación (el panel las reemplaza/usa) ----------------
def log(*a):
    """Salida de texto. El panel gráfico reemplaza esta función por la suya."""
    print(*a, flush=True)


CANCELAR = threading.Event()


class Cancelado(Exception):
    """Se lanza cuando el usuario pulsa DETENER."""


def chequear():
    if CANCELAR.is_set():
        raise Cancelado()


# ---------------- CONFIG DEL MENSAJE (edita esto) ----------------
MI_NOMBRE = "webflux"  # cómo te presentas
OFERTA = (
    "páginas web para negocios y automatización de procesos "
    "(respuestas, documentos y correos) para negocios y emprendimientos"
)
MODELO = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
PAUSA_API = 2.2          # segundos entre llamadas a Groq
DIAS_SEGUIMIENTO = 3     # días sin respuesta antes de generar follow-up
MAX_RESULTADOS = 200     # tope de negocios por búsqueda en Maps
ARCHIVO_LINKS = "linkswame.txt"

# Variantes A/B: cambia solo la pregunta final
VARIANTES = {
    "A": "La pregunta final debe ser si les interesaría saber cómo podrías ayudarles.",
    "B": "La pregunta final debe ser cuál es la tarea repetitiva que más tiempo les quita en el día.",
}
# -----------------------------------------------------------------


def sistema(variante):
    return f"""Eres {MI_NOMBRE}, una empresa independiente de Colombia que escribe por WhatsApp a dueños de negocios locales. Ofreces: {OFERTA}.
Escribe UN primer mensaje de contacto. Reglas:
- Máximo 4 frases cortas. Tono cercano y respetuoso, español colombiano, sin sonar a vendedor ni a plantilla.
- Salúdalo usando el nombre del negocio y menciona algo específico según su tipo de negocio y su presencia online.
- Preséntate con tu nombre.
- Si el negocio no tiene página web, puedes mencionar que una le ayudaría.
- Si tiene página y hay hallazgos reales, menciona UNO solo, con tacto y sin criticar, y ofrece ayuda. Si no hay hallazgos, NO le ofrezcas una página: enfócate en ahorrarle trabajo repetitivo.
- Nunca inventes datos que no estén en la información dada.
- Termina con UNA pregunta simple. {VARIANTES[variante]} No des precios ni vendas todavía.
- Incluye una salida amable (por ejemplo, que si no es el momento no hay problema).
- Máximo 1 emoji. Sin hashtags, sin comillas, sin markdown.
- Responde SOLO con un JSON válido con esta forma: {{"mensaje": "texto del mensaje"}}"""


SISTEMA_FOLLOWUP = f"""Eres {MI_NOMBRE}, una empresa independiente de Colombia. Hace unos días le escribiste por WhatsApp a un dueño de negocio y no ha respondido.
Escribe UN mensaje de seguimiento. Reglas:
- Máximo 2 frases cortas, tono amable, sin presionar ni hacerlo sentir culpable.
- Recuerda brevemente quién eres y usa el nombre del negocio.
- Incluye una salida amable (si no es el momento, sin problema).
- Máximo 1 emoji. Sin comillas, sin markdown.
- Responde SOLO con un JSON válido con esta forma: {{"mensaje": "texto del mensaje"}}"""


# ---------------------- utilidades ----------------------

def normalizar_tel(tel):
    """Celular colombiano con indicativo (57...) o None si no sirve para WhatsApp."""
    d = re.sub(r"\D", "", tel or "")
    if len(d) == 10 and d.startswith("3"):
        return "57" + d
    if len(d) == 12 and d.startswith("573"):
        return d
    return None


REDES = (
    "instagram.com", "facebook.com", "fb.com", "fb.me", "tiktok.com",
    "twitter.com", "x.com", "youtube.com", "youtu.be", "wa.me",
    "wa.link", "whatsapp.com", "linktr.ee", "beacons.ai", "bio.link",
    "taplink.cc", "linkin.bio",
)


def es_red_social(url):
    host = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.")
    return any(host == r or host.endswith("." + r) for r in REDES)


def place_id_de(url):
    """ID interno de Maps (0x...:0x...) sacado del enlace del negocio."""
    m = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", url or "")
    return m.group(1) if m else None


def presencia(web, social):
    if web:
        return "tiene página web"
    if social:
        return "solo tiene red social, sin página web propia"
    return "no tiene página web ni redes sociales visibles"


def wame(tel, mensaje):
    return f"https://wa.me/{tel}?text={urllib.parse.quote(mensaje)}"


# ---------------------- mensajes (Groq) ----------------------

def mensaje_respaldo(nombre):
    """Se usa si Groq falla o no hay API key."""
    return (
        f"Hola, ¿cómo están en {nombre}? somos {MI_NOMBRE}, hago {OFERTA}. "
        "¿Les interesaría que les cuente cómo podría ayudarles? "
        "Si no es el momento, sin problema."
    )


def followup_respaldo(nombre):
    return (
        f"Hola de nuevo, somos {MI_NOMBRE}. Les escribí hace unos días por {nombre}; "
        "si les interesa, con gusto les cuento. Si no es el momento, sin problema."
    )


def extraer_mensaje(raw):
    """Saca el texto del JSON {"mensaje": ...}. Tolera respuestas en texto plano."""
    raw = (raw or "").strip()
    texto = None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            texto = obj.get("mensaje")
    except ValueError:
        if raw and not raw.startswith("{"):
            texto = raw
    texto = (texto or "").strip().strip('"').strip()
    return texto if texto and len(texto) <= 700 else None


def generar_mensaje(client, system, usuario, respaldo, etiqueta=""):
    """Pide a Groq un mensaje. Reintenta con backoff. Nunca lanza excepción."""
    if client is None:
        return respaldo
    extra = {"reasoning_effort": "low"} if "gpt-oss" in MODELO else {}

    for intento in range(3):
        try:
            kw = dict(extra)
            if intento < 2:  # el último intento va sin modo JSON, por si el modelo lo rechaza
                kw["response_format"] = {"type": "json_object"}
            r = client.chat.completions.create(
                model=MODELO,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": usuario},
                ],
                temperature=0.9,
                max_tokens=1000,
                **kw,
            )
            texto = extraer_mensaje(r.choices[0].message.content)
            if texto:
                return texto
            log(f"  Respuesta vacía/inválida para '{etiqueta}' (intento {intento + 1})")
        except Exception as e:
            log(f"  Groq falló para '{etiqueta}' (intento {intento + 1}): {e}")
        if intento < 2:
            time.sleep(min(2 ** (intento + 1), 20) + random.random())
    return respaldo


# ---------------------- scraping de Maps ----------------------

JS_EXTRAER = r"""
els => els.map(e => {
    const a = e.querySelector('a.hfpxzc');
    const txt = e.innerText || '';
    const tel = [...e.querySelectorAll('span.UsdlK')]
                    .map(s => s.innerText).find(t => t.trim())
             || (txt.match(/(?:\+?57[\s-]?)?3\d{2}[\s-]?\d{3}[\s-]?\d{4}/) || [])[0];
    const web = e.querySelector('a[data-value="Sitio web"]')?.href
             ?? e.querySelector('a[aria-label^="Sitio web"]')?.href
             ?? "";
    return {
        nombre: e.querySelector('div.qBF1Pd')?.innerText || a?.getAttribute('aria-label') || '',
        tel: tel || '',
        web: web,
        maps_url: a?.href || ''
    };
})
"""


def scrape_maps(browser, servicio, lugar, max_resultados=MAX_RESULTADOS):
    """Devuelve la lista cruda de negocios de una búsqueda en Maps."""
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    context.route(
        "**/*",
        lambda r: r.abort()
        if r.request.resource_type in ("image", "font", "media")
        else r.continue_(),
    )
    page = context.new_page()
    url = f"https://www.google.com/maps/search/{urllib.parse.quote(f'{servicio} en {lugar}')}"
    log(f"\nNavegando a: {url}")

    try:
        page.goto(url, timeout=60000)
        log("Esperando resultados...")
        page.wait_for_selector('div[role="feed"]', timeout=30000)

        previo = 0
        for _ in range(80):  # tope de seguridad
            chequear()
            page.evaluate(
                "document.querySelector('div[role=\"feed\"]').scrollTo(0, 1e9)"
            )
            try:
                page.wait_for_function(
                    "n => document.querySelectorAll('div[role=\"article\"]').length > n",
                    arg=previo,
                    timeout=4000,
                )
            except PWTimeout:
                log("No hay más resultados")
                break
            previo = page.locator('div[role="article"]').count()
            log(f"Cargados: {previo}")
            if previo >= max_resultados:
                log(f"Tope de {max_resultados} alcanzado")
                break

        datos = page.eval_on_selector_all('div[role="article"]', JS_EXTRAER)
        log(f"Artículos encontrados: {len(datos)}")
        return datos
    except Cancelado:
        raise
    except Exception as e:
        log(f"Error en Maps: {e}")
        try:
            page.screenshot(path="error_debug.png")
        except Exception:
            pass
        return []
    finally:
        context.close()


# ---------------------- flujo principal ----------------------

def mensaje_para(client, nombre, tipo, web, red_social, hallazgos, variante):
    """Genera el primer mensaje de un prospecto (con Groq o el respaldo)."""
    pts = auditoria.resumir(hallazgos)
    usuario = (
        f"Negocio: {nombre}\n"
        f"Tipo de negocio: {tipo}\n"
        f"Presencia online: {presencia(web, red_social)}\n"
        f"Hallazgos reales de su página: {'; '.join(pts) if pts else 'ninguno'}"
    )
    return generar_mensaje(
        client, sistema(variante), usuario, mensaje_respaldo(nombre), nombre
    )


def regenerar_mensaje(conn, client, r):
    """Genera de nuevo el mensaje de un prospecto ya guardado (r = fila de la base)."""
    variante = r.get("variante") or random.choice(list(VARIANTES))
    log(f"Generando mensaje para: {r['nombre']}")
    msg = mensaje_para(
        client, r["nombre"], r.get("tipo"), r.get("web"), r.get("red_social"),
        r.get("hallazgos"), variante,
    )
    db.guardar_mensaje(conn, r["id"], variante, msg)
    log("Mensaje listo.")
    return msg


def _cerrar_seccion(conn, sid, n, estado):
    """Marca cómo terminó la sección. Nunca lanza excepción."""
    try:
        conn.rollback()
        db.finalizar_scrape(conn, sid, n, estado)
    except Exception as e:
        log(f"(no pude cerrar la sección {sid}: {e})")


def procesar(conn, client, servicio, lugar, args):
    """Una búsqueda = una sección (scrape) con sus propios leads."""
    # El navegador vive solo durante el scraping; la auditoría va después, sin él.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not getattr(args, "debug", False))
        try:
            datos = scrape_maps(browser, servicio, lugar, args.max)
        finally:
            browser.close()
    chequear()
    if not datos:
        log(f"Maps no devolvió resultados para '{servicio}' en {lugar}.")
        return

    sid = db.crear_scrape(conn, servicio, lugar, args.max, len(datos))
    nuevos = []  # se llena a medida que se guardan, así sabemos cuántos hubo si algo falla
    try:
        _guardar_y_mensajes(conn, client, servicio, lugar, args, datos, sid, nuevos)
    except Cancelado:
        _cerrar_seccion(conn, sid, len(nuevos), "cancelado")
        raise
    except Exception:
        _cerrar_seccion(conn, sid, len(nuevos), "error")
        raise
    _cerrar_seccion(conn, sid, len(nuevos), "ok")


def _guardar_y_mensajes(conn, client, servicio, lugar, args, datos, sid, nuevos):
    descartados = 0
    for d in datos:
        if not d["nombre"] or not d["tel"]:
            continue
        tel = normalizar_tel(d["tel"])
        if not tel:
            descartados += 1
            continue
        url = d["web"]
        social = url if url and es_red_social(url) else ""
        web = "" if social else url
        fila = {
            "scrape_id": sid,
            "place_id": place_id_de(d["maps_url"]),
            "nombre": d["nombre"],
            "telefono": tel,
            "tipo": servicio,
            "lugar": lugar,
            "web": web or None,
            "red_social": social or None,
            "maps_url": d["maps_url"] or None,
        }
        pid = db.insertar(conn, fila)
        if pid:
            fila["id"] = pid
            nuevos.append(fila)

    if not nuevos:
        log(f"Sin contactos nuevos ({descartados} descartados por fijo/sin WhatsApp, "
            "o ya existían en otra sección).")
        return

    # 1) Auditoría de las webs (en paralelo)
    con_web = [n for n in nuevos if n["web"]]
    chequear()
    if con_web and not args.sin_auditoria:
        log(f"Auditando {len(con_web)} páginas web...")
        resultados = asyncio.run(
            auditoria.auditar_todos(
                [n["web"] for n in con_web],
                pagespeed=not args.sin_pagespeed,
                api_key=os.getenv("PAGESPEED_API_KEY"),
            )
        )
        for n, h in zip(con_web, resultados):
            n["hallazgos"] = h
            email = (h.get("emails") or [None])[0]
            red = h.get("instagram") or h.get("facebook")
            db.guardar_auditoria(conn, n["id"], h, email, red)

    # 2) Un mensaje distinto por negocio
    lineas = []
    for i, n in enumerate(nuevos, 1):
        chequear()  # permite DETENER durante la fase larga de mensajes
        log(f"Generando mensaje {i}/{len(nuevos)}: {n['nombre']}")
        variante = random.choice(list(VARIANTES))
        msg = mensaje_para(
            client, n["nombre"], servicio, n["web"], n["red_social"],
            n.get("hallazgos"), variante,
        )
        db.guardar_mensaje(conn, n["id"], variante, msg)
        lineas.append(f"[{n['id']}] {n['nombre']} -> {wame(n['telefono'], msg)}")
        if client is not None:
            time.sleep(PAUSA_API)

    with open(ARCHIVO_LINKS, "a", encoding="utf-8") as f:
        f.write("\n".join(lineas) + "\n")

    sin_web = sum(1 for n in nuevos if not n["web"])
    log(f"\n¡Terminado! {len(nuevos)} prospectos nuevos de '{servicio}' en {lugar}.")
    log(f"  - Sin página web (mejores leads): {sin_web}")
    log(f"  - Descartados (fijos/sin WhatsApp): {descartados}")
    log(f"  - Links también guardados en '{ARCHIVO_LINKS}'")


def crear_client():
    """Cliente de Groq, o None si no hay GROQ_API_KEY (se usan mensajes de respaldo)."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        log("AVISO: falta GROQ_API_KEY, uso mensajes fijos de respaldo.")
        return None
    return Groq(api_key=api_key)


def run_busquedas(conn, client, busquedas, args):
    """Ejecuta una o varias búsquedas [(negocio, lugar), ...]."""
    for i, (negocio, lugar) in enumerate(busquedas):
        chequear()
        if i:  # pausa entre búsquedas, cancelable
            for _ in range(random.randint(10, 20)):
                chequear()
                time.sleep(0.5)
        procesar(conn, client, negocio, lugar, args)


def seguimientos(conn, client):
    pend = db.pendientes_seguimiento(conn, DIAS_SEGUIMIENTO)
    if not pend:
        log(f"No hay prospectos con {DIAS_SEGUIMIENTO}+ días sin respuesta.")
        return
    log(f"{len(pend)} prospectos para seguimiento:\n")
    for p in pend:
        chequear()
        usuario = (
            f"Negocio: {p['nombre']}\nTipo de negocio: {p['tipo']}\n"
            f"Mensaje anterior: {p['mensaje']}"
        )
        msg = generar_mensaje(
            client, SISTEMA_FOLLOWUP, usuario, followup_respaldo(p["nombre"]), p["nombre"]
        )
        db.guardar_followup(conn, p["id"], msg)
        log(f"[{p['id']}] {p['nombre']} -> {wame(p['telefono'], msg)}\n")
        if client is not None:
            time.sleep(PAUSA_API)


def listar(conn, estado):
    filas = db.listar(conn, None if estado == "todos" else estado)
    if not filas:
        log("Sin resultados.")
        return
    for r in filas:
        log(
            f"{r['id']:>4}  S{r.get('scrape_id') or '-':<3} {r['estado']:<11} {r['variante'] or '-':<2} "
            f"{r['nombre'][:32]:<32} {r['telefono']}  {r['email'] or ''}"
        )


def stats(conn):
    por_estado, por_variante = db.estadisticas(conn)
    log("Embudo:")
    for r in por_estado:
        log(f"  {r['estado']:<11} {r['total']}")
    log("\nA/B (respondieron / contactados):")
    for r in por_variante:
        c = r["contactados"]
        pct = f"{100 * r['respondieron'] / c:.0f}%" if c else "-"
        log(f"  Variante {r['variante']}: {r['respondieron']}/{c}  ({pct})")


def main():
    ap = argparse.ArgumentParser(
        description="Scraper de negocios en Google Maps -> Postgres",
        epilog='lote.json: {"negocios": ["restaurantes"], "lugares": ["Aguachica", "Ocaña"]}',
    )
    ap.add_argument("negocio", nargs="?", help="tipo de negocio, ej: restaurantes")
    ap.add_argument("lugar", nargs="?", help="ciudad, zona o barrio, ej: Aguachica")
    ap.add_argument("--lote", metavar="ARCHIVO", help="JSON con negocios × lugares")
    ap.add_argument("--debug", action="store_true", help="muestra el navegador")
    ap.add_argument("--max", type=int, default=MAX_RESULTADOS, help="tope de resultados")
    ap.add_argument("--sin-auditoria", action="store_true", help="no auditar las webs")
    ap.add_argument("--sin-pagespeed", action="store_true", help="auditar sin PageSpeed")
    ap.add_argument("--seguimientos", action="store_true", help="generar follow-ups")
    ap.add_argument("--listar", nargs="?", const="todos", metavar="ESTADO")
    ap.add_argument("--estado", nargs=2, metavar=("ID", "ESTADO"))
    ap.add_argument("--stats", action="store_true", help="embudo y resultados A/B")
    a = ap.parse_args()

    try:
        conn = db.conectar()
    except psycopg2.OperationalError as e:
        sys.exit(f"No pude conectar a Postgres: {e}\nRevisa tu .env y que el contenedor esté arriba (docker ps).")
    db.init_db(conn)

    # --- comandos que no scrapean ---
    if a.estado:
        try:
            n = db.cambiar_estado(conn, int(a.estado[0]), a.estado[1])
        except (ValueError, psycopg2.Error) as e:
            sys.exit(f"Error: {e}")
        log("Actualizado." if n else "No existe ese ID.")
        return
    if a.listar is not None:
        return listar(conn, a.listar)
    if a.stats:
        return stats(conn)

    client = crear_client()

    if a.seguimientos:
        return seguimientos(conn, client)

    # --- scraping ---
    if a.lote:
        with open(a.lote, encoding="utf-8") as f:
            cfg = json.load(f)
        busquedas = [(n, l) for n in cfg["negocios"] for l in cfg["lugares"]]
    else:
        negocio = a.negocio or input("ingrese el tipo de negocio = ")
        lugar = a.lugar or input("ingrese el lugar = ")
        busquedas = [(negocio, lugar)]

    run_busquedas(conn, client, busquedas, a)
    conn.close()


if __name__ == "__main__":
    main()
