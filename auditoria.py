"""Auditoría asíncrona de páginas web de los prospectos."""
import asyncio
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PAGESPEED = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

STACKS = [
    ("WordPress", ("wp-content", "wp-includes")),
    ("Wix", ("wixstatic.com", "static.parastorage.com")),
    ("Squarespace", ("squarespace",)),
    ("Shopify", ("cdn.shopify.com",)),
    ("Webflow", ("webflow",)),
    ("Blogger", ("blogger.com", "blogspot")),
    ("Joomla", ("/media/jui/", "joomla")),
]

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_BASURA = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
EMAIL_IGNORAR = ("sentry", "wixpress", "example", "tuemail", "tucorreo", "dominio.")
IG_IGNORAR = {"p", "reel", "reels", "explore", "accounts", "sharer", "share", "tv"}
FB_IGNORAR = {"sharer", "sharer.php", "share.php", "tr", "plugins", "dialog", "login"}


def normalizar_url(url):
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def _emails(texto):
    vistos = []
    for e in RE_EMAIL.findall(texto):
        e = e.lower().rstrip(".")
        if e.endswith(EMAIL_BASURA) or any(x in e for x in EMAIL_IGNORAR):
            continue
        if e not in vistos:
            vistos.append(e)
    return vistos[:3]


def analizar_html(html, url_final):
    """Extrae hallazgos de una página a partir de su HTML."""
    low = html.lower()
    h = {
        "https": url_final.startswith("https://"),
        "responsive": bool(re.search(r'<meta[^>]+name=["\']viewport["\']', low)),
        "whatsapp": any(
            k in low
            for k in ("wa.me/", "api.whatsapp.com", "web.whatsapp.com", "whatsapp.com/send")
        ),
        "formulario": "<form" in low,
    }

    anios = [
        int(a)
        for a in re.findall(r"(?:©|&copy;|copyright)[^<\n]{0,40}?((?:19|20)\d{2})", low)
    ]
    h["copyright"] = max(anios) if anios else None
    h["stack"] = next(
        (n for n, claves in STACKS if any(k in low for k in claves)), None
    )
    h["emails"] = _emails(html)

    ig = next(
        (u for u in re.findall(r"instagram\.com/([A-Za-z0-9_.]+)", html)
         if u.lower() not in IG_IGNORAR),
        None,
    )
    fb = next(
        (u for u in re.findall(r"facebook\.com/([A-Za-z0-9_.\-]+)", html)
         if u.lower() not in FB_IGNORAR),
        None,
    )
    h["instagram"] = f"https://instagram.com/{ig}" if ig else None
    h["facebook"] = f"https://facebook.com/{fb}" if fb else None
    return h


async def _descargar(seguro, inseguro, url):
    """GET con reintento sin verificar certificado. Devuelve (respuesta, ssl_invalido)."""
    try:
        return await seguro.get(url), False
    except httpx.ConnectError as e:
        msg = str(e).upper()
        if "CERTIFICATE" in msg or "SSL" in msg:
            return await inseguro.get(url), True
        raise


async def auditar_html(seguro, inseguro, url):
    url = normalizar_url(url)
    h = {"url": url, "error": None}
    try:
        t0 = time.perf_counter()
        r, ssl_malo = await _descargar(seguro, inseguro, url)
        h["tiempo_s"] = round(time.perf_counter() - t0, 2)
        h["status"] = r.status_code
        h["ssl_invalido"] = ssl_malo
        if r.status_code >= 400:
            h["error"] = f"HTTP {r.status_code}"
        html = r.text
        h.update(analizar_html(html, str(r.url)))
        h["url"] = str(r.url)

        # Si no hay email en el home, probar la página de contacto
        if not h["emails"]:
            m = re.search(r'href=["\']([^"\']*contact[^"\']*)["\']', html, re.I)
            if m:
                contacto = urljoin(str(r.url), m.group(1))
                if urlparse(contacto).hostname == urlparse(str(r.url)).hostname:
                    try:
                        rc = await (inseguro if ssl_malo else seguro).get(contacto)
                        h["emails"] = _emails(rc.text)
                    except httpx.HTTPError:
                        pass
    except Exception as e:  # una web rota nunca debe tumbar el lote
        h["error"] = type(e).__name__
    return h


async def pagespeed_movil(cliente, url, api_key=None):
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if api_key:
        params["key"] = api_key
    try:
        r = await cliente.get(PAGESPEED, params=params, timeout=90)
        if r.status_code != 200:
            return {"perf_movil": None}
        lh = r.json()["lighthouseResult"]
        score = lh["categories"]["performance"]["score"]
        return {
            "perf_movil": int(round(score * 100)) if score is not None else None,
            "lcp": lh["audits"]["largest-contentful-paint"].get("displayValue"),
        }
    except (httpx.HTTPError, KeyError, ValueError):
        return {"perf_movil": None}


async def auditar_todos(urls, pagespeed=True, api_key=None, concurrencia=8):
    """Audita varias webs en paralelo. Devuelve una lista alineada con `urls`."""
    sem_html = asyncio.Semaphore(concurrencia)
    sem_ps = asyncio.Semaphore(3)  # PageSpeed es lento y tiene cuota
    cab = {"User-Agent": UA, "Accept-Language": "es-CO,es;q=0.9"}

    async with httpx.AsyncClient(
        headers=cab, timeout=15, follow_redirects=True
    ) as seguro, httpx.AsyncClient(
        headers=cab, timeout=15, follow_redirects=True, verify=False
    ) as inseguro:

        async def uno(u):
            async with sem_html:
                h = await auditar_html(seguro, inseguro, u)
            if pagespeed and not h.get("error"):
                async with sem_ps:
                    h.update(await pagespeed_movil(seguro, h["url"], api_key))
            return h

        return await asyncio.gather(*(uno(u) for u in urls))


def resumir(h):
    """Hasta 3 hallazgos reales, en español y con tacto, para el prompt."""
    if not h:
        return []
    pts = []
    if h.get("error"):
        pts.append("su página web tuvo problemas para abrir al revisarla")
    if h.get("ssl_invalido"):
        pts.append("el certificado de seguridad de su página está vencido o es inválido")
    perf = h.get("perf_movil")
    if perf is not None and perf < 50:
        lcp = f", tarda {h['lcp']} en mostrar lo principal" if h.get("lcp") else ""
        pts.append(f"en celular carga lento (puntaje {perf}/100{lcp})")
    if h.get("responsive") is False:
        pts.append("la página no se adapta bien al celular")
    if h.get("https") is False:
        pts.append("la página no tiene HTTPS (sale como 'no segura' en el navegador)")
    if h.get("whatsapp") is False:
        pts.append("la página no tiene botón de WhatsApp")
    cr = h.get("copyright")
    if cr and cr <= datetime.now().year - 3:
        pts.append(f"el pie de página todavía dice {cr}")
    return pts[:3]
