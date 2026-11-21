"""Lectura de la ficha de Amazon Espana (/dp/ASIN) e interpretacion de su HTML.

Fuente: la ficha publica del producto, leida con un Chromium real como un
visitante mas, a baja frecuencia (por defecto cada 12 h por producto). Es el
metodo del bot desde octubre de 2026. No reutiliza cookies ni sesiones, y ante
un CAPTCHA o un error no insiste: se registra el fallo y se espera al
siguiente ciclo (ver README, "Fuentes y autorizacion").

La condicion NO se deduce del texto "En stock": Amazon lo muestra tambien
cuando la unica oferta es de segunda mano. Se usa el JSON de opciones de
compra de la propia ficha ({"buyingOptionTypes":["NEW"|"USED"|...]}).
"""
from __future__ import annotations

import html as html_mod
import re
import time

import check_price as cp

from .modelo import NUEVO, REACONDICIONADO, USADO, Observacion, a_centimos, ahora_utc

FUENTE = "amazon_ficha"


class ErrorLectura(RuntimeError):
    """La pagina no trae un precio interpretable (no es un precio, es un fallo)."""


class ErrorBloqueo(ErrorLectura):
    """CAPTCHA o pagina de bloqueo, aunque llegue con HTTP 200."""


_OPCIONES = re.compile(r'"buyingOptionTypes":\[([^\]]*)\][^}]*?"productAsin":"([A-Z0-9]{10})"')
_OTRA_OFERTA = re.compile(r"BuyingOptionData\(buyingOptionType=(USED|RENEWED\w*), price=([\d.,]+)")
_TITULO = re.compile(r'id="productTitle"[^>]*>\s*([^<]+?)\s*<')
_IMAGEN_HIRES = re.compile(r'data-old-hires="(https://m\.media-amazon\.com/images/I/[^"]+)"')
_IMAGEN_JSON = re.compile(r'"hiRes":"(https://m\.media-amazon\.com/images/I/[^"]+)"')
_VENDEDOR = re.compile(r"id=['\"]sellerProfileTriggerId['\"][^>]*>\s*([^<]+?)\s*<")
_VENDIDO_POR = re.compile(r"(?:Vendido por|Vendedor)\s*</span>[\s\S]{0,500}?"
                          r"offer-display-feature-text-message[^>]*>\s*([^<]+?)\s*<")
_VALORACION = re.compile(r'id="acrPopover"[^>]*title="([\d,]+) de 5 estrellas"')
_RESENAS = re.compile(r'id="acrCustomerReviewText"[^>]*aria-label="([\d.]+) [^"]*"')
_MIGAS = re.compile(r'wayfinding-breadcrumbs[\s\S]*?</ul>')
_MIGA = re.compile(r'<a [^>]*>\s*([^<]+?)\s*</a>')
_AGOTADO = re.compile(r'id="outOfStock"|>\s*No disponible por el momento\.?\s*<')

_CONDICION = {"NEW": NUEVO, "USED": USADO}


def _condicion(tipo: str) -> str:
    return REACONDICIONADO if tipo.startswith("RENEWED") else _CONDICION.get(tipo, tipo.lower())


def opciones_compra(html: str, asin: str) -> list[str] | None:
    """Tipos de oferta de ESTE asin (None si la ficha no lo dice)."""
    for m in _OPCIONES.finditer(html):
        if m.group(2) == asin:
            return re.findall(r'"(\w+)"', m.group(1))
    return None


def analizar_ficha(html: str, asin: str, fecha=None) -> Observacion:
    """Convierte el HTML de la ficha en una Observacion o lanza ErrorLectura."""
    if cp._es_captcha_amazon(html):
        raise ErrorBloqueo("Amazon ha servido un CAPTCHA en lugar de la ficha")
    if asin not in html:
        raise ErrorLectura("La pagina no es la ficha de %s (redireccion u otra variante)" % asin)

    obs = Observacion(clave=asin, fecha=fecha or ahora_utc(), fuente=FUENTE)
    _metadatos(html, obs)

    tipos = opciones_compra(html, asin)
    otra = _OTRA_OFERTA.search(html)
    if otra:
        obs.otra_condicion = _condicion(otra.group(1))
        obs.precio_otra_condicion_cent = a_centimos(otra.group(2))

    if tipos is not None and "NEW" not in tipos:
        # Solo segunda mano / reacondicionado: no hay precio NUEVO que registrar.
        obs.disponible = False
        obs.precio_cent = None
        if tipos and not otra:
            obs.otra_condicion = _condicion(tipos[0])
        return obs

    precio = cp._extraer_precio_amazon(html)
    if precio is None:
        if _AGOTADO.search(html):
            obs.disponible = False
            return obs
        titulo = re.search(r"<title>([^<]*)</title>", html)
        raise ErrorLectura("La ficha no trae el precio (%d bytes, titulo %r)"
                           % (len(html), titulo.group(1).strip()[:60] if titulo else ""))
    if precio <= 0:
        raise ErrorLectura("Precio sospechoso (%s)" % precio)

    obs.precio_cent = a_centimos(precio)
    final, texto = cp._cupon_amazon(html, precio)
    if texto:
        obs.cupon_cent = obs.precio_cent - a_centimos(final)
        obs.cupon_texto = texto
    return obs


def _metadatos(html: str, obs: Observacion) -> None:
    m = _TITULO.search(html)
    obs.titulo = html_mod.unescape(m.group(1)) if m else None
    m = _IMAGEN_HIRES.search(html) or _IMAGEN_JSON.search(html)
    obs.imagen = m.group(1) if m else None
    m = _VENDEDOR.search(html) or _VENDIDO_POR.search(html)
    obs.vendedor = html_mod.unescape(m.group(1)) if m else None
    m = _VALORACION.search(html)
    obs.valoracion = float(m.group(1).replace(",", ".")) if m else None
    m = _RESENAS.search(html)
    obs.num_valoraciones = int(m.group(1).replace(".", "")) if m else None
    m = _MIGAS.search(html)
    if m:
        migas = _MIGA.findall(m.group(0))
        obs.categoria = html_mod.unescape(migas[-1]) if migas else None


# --------------------------------------------------------------------------
# Lectura


def _listo(html: str, asin: str) -> bool:
    return (cp._extraer_precio_amazon(html) is not None or cp._es_captcha_amazon(html)
            or opciones_compra(html, asin) is not None or bool(_AGOTADO.search(html)))


def descargar_ficha(asin: str, espera_max_seg: float = 20) -> str:
    """HTML de la ficha. Con AMAZON_MODO=http, peticion simple; si no, Chromium."""
    url = "https://www.amazon.es/dp/%s" % asin
    if cp._modo(cp.AMAZON_MODO) == "http":
        return cp._descargar(url, {"User-Agent": cp.UA, "Accept-Language": "es-ES,es;q=0.9"}) \
            .decode("utf-8", errors="ignore")
    with cp._pagina_navegador() as pagina:
        respuesta = pagina.goto(url, wait_until="domcontentloaded", timeout=45000)
        if respuesta is not None and respuesta.status >= 400:
            raise ErrorBloqueo("Amazon ha respondido HTTP %d" % respuesta.status)
        limite = time.monotonic() + espera_max_seg
        html = ""
        while True:
            try:
                html = pagina.content()
            except Exception:  # noqa: BLE001 - la pagina sigue navegando
                html = ""
            if _listo(html, asin) or time.monotonic() >= limite:
                return html
            pagina.wait_for_timeout(1000)


def leer(asin: str) -> Observacion:
    """Una lectura, con un unico reintento ante un fallo puntual. Ante un
    bloqueo no se reintenta enseguida: se deja para el siguiente ciclo."""
    try:
        return analizar_ficha(descargar_ficha(asin), asin)
    except ErrorBloqueo:
        raise
    except cp.ErrorHTTP as exc:
        if exc.status in (403, 429, 503):
            raise ErrorBloqueo("Amazon ha respondido HTTP %d: se deja para el siguiente ciclo"
                               % exc.status) from exc
        raise
    except Exception:  # noqa: BLE001
        time.sleep(cp.ESPERA_ENTRE_REINTENTOS_SEG)
        return analizar_ficha(descargar_ficha(asin), asin)
