"""Lectura del precio de un producto en cada tienda.

Tres lectores, todos con peticiones normales a fichas publicas:

- amazon_ficha: la ficha de Amazon con un Chromium real (ver amazon.py).
- jsonld: cualquier tienda que publique el producto en datos estructurados
  schema.org (JSON-LD), como Powerplanet o MediaMarkt. Es el formato que las
  tiendas ponen para buscadores. Una peticion HTTP, sin navegador.
- shopify: tiendas Shopify (p. ej. la oficial de Dreame), con el JSON publico
  del producto (/products/<handle>.js).

Las peticiones se identifican como el bot (User-Agent con enlace al repo). Un
403, 429, 503 o un desafio anti-bots no es un precio: se registra como fallo y
se espera al siguiente ciclo, sin intentar superarlo.

Si el producto tiene EAN, se comprueba que la ficha sea exactamente esa
referencia: una variante de otro color o modelo da error en vez de colar un
precio que no toca.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import amazon
from .fuentes import UA
from .modelo import Observacion, Tienda, a_centimos, ahora_utc

LECTORES = ("amazon_ficha", "jsonld", "shopify")
DESAFIO = re.compile(r"Just a moment|cf-chl|challenge-platform|Verify you are human|/errors/validateCaptcha")
DISPONIBLE = ("InStock", "LimitedAvailability", "OnlineOnly", "InStoreOnly")


class ErrorTienda(RuntimeError):
    """La ficha no trae un precio valido para este producto."""


class BloqueoTienda(ErrorTienda):
    """La tienda ha denegado el acceso (403/429/503 o desafio)."""


def _descargar(url: str, acepta: str = "text/html") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": acepta,
                                               "Accept-Language": "es-ES,es;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            texto = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429, 503):
            raise BloqueoTienda("%s responde HTTP %d" % (urlsplit(url).netloc, exc.code)) from exc
        raise ErrorTienda("%s responde HTTP %d" % (urlsplit(url).netloc, exc.code)) from exc
    if DESAFIO.search(texto[:20000]):
        raise BloqueoTienda("%s ha servido un desafio anti-bots" % urlsplit(url).netloc)
    return texto


# ------------------------------------------------------------------- JSON-LD
def _productos_ld(html: str):
    """Todos los Product con offers del JSON-LD, esten donde esten (BuyAction,
    ProductGroup.hasVariant, @graph...)."""
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            pila = [json.loads(m.group(1))]
        except json.JSONDecodeError:
            continue
        while pila:
            x = pila.pop()
            if isinstance(x, list):
                pila.extend(x)
            elif isinstance(x, dict):
                if x.get("@type") in ("Product", ["Product"]) and x.get("offers"):
                    yield x
                pila.extend(v for v in x.values() if isinstance(v, (dict, list)))


def _ean(p: dict) -> str | None:
    for k in ("gtin13", "gtin", "gtin14", "gtin12", "ean"):
        if p.get(k):
            return str(p[k]).lstrip("0")
    return None


def _ofertas(p: dict) -> list[dict]:
    of = p["offers"]
    if isinstance(of, dict) and of.get("@type") == "AggregateOffer" and of.get("offers"):
        of = of["offers"]
    return of if isinstance(of, list) else [of]


def analizar_jsonld(html: str, t: Tienda, ean: str | None, fecha=None) -> Observacion:
    productos = list(_productos_ld(html))
    if not productos:
        raise ErrorTienda("La ficha de %s no trae datos de producto (JSON-LD)" % t.nombre)
    if ean:
        productos = [p for p in productos if _ean(p) == ean.lstrip("0")]
        if not productos:
            raise ErrorTienda("La ficha de %s no es la del EAN %s (otra variante o modelo)" % (t.nombre, ean))
    p = productos[0]
    obs = Observacion(clave=t.clave, fecha=fecha or ahora_utc(), fuente="jsonld", disponible=False,
                      titulo=(p.get("name") or "").strip() or None)
    img = p.get("image")
    obs.imagen = (img[0] if isinstance(img, list) and img else img) if isinstance(img, (str, list)) else None
    mejores = []
    for o in _ofertas(p):
        estado = str(o.get("availability", "")).rsplit("/", 1)[-1]
        condicion = str(o.get("itemCondition", "NewCondition")).rsplit("/", 1)[-1]
        if condicion not in ("NewCondition", "") or o.get("priceCurrency", "EUR") != "EUR":
            continue
        try:
            precio = a_centimos(float(str(o.get("price", "")).replace(",", ".")))
        except ValueError:
            continue
        if precio <= 0:
            continue
        vendedor = o.get("seller")
        vendedor = vendedor.get("name") if isinstance(vendedor, dict) else vendedor
        mejores.append((estado in DISPONIBLE, -precio, precio, vendedor))
    if not mejores:
        raise ErrorTienda("La ficha de %s no trae un precio de producto nuevo" % t.nombre)
    disponible, _, precio, vendedor = max(mejores)  # la disponible mas barata
    obs.disponible = disponible
    obs.precio_cent = precio if disponible else None
    obs.vendedor = vendedor
    return obs


# ------------------------------------------------------------------- Shopify
def _url_shopify(url: str) -> str:
    base = url.split("?")[0].rstrip("/")
    return base if base.endswith(".js") else base + ".js"


def analizar_shopify(texto: str, t: Tienda, ean: str | None, fecha=None) -> Observacion:
    try:
        d = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ErrorTienda("La tienda %s no ha devuelto el JSON del producto" % t.nombre) from exc
    variantes = d.get("variants") or []
    if t.extra.get("variant_id"):
        variantes = [v for v in variantes if str(v.get("id")) == str(t.extra["variant_id"])]
    elif ean:
        variantes = [v for v in variantes if str(v.get("barcode") or "").lstrip("0") == ean.lstrip("0")]
    if not variantes:
        raise ErrorTienda("La tienda %s no tiene la variante del EAN %s" % (t.nombre, ean))
    v = variantes[0]
    obs = Observacion(clave=t.clave, fecha=fecha or ahora_utc(), fuente="shopify",
                      disponible=bool(v.get("available")), titulo=d.get("title"), vendedor=t.nombre)
    obs.precio_cent = int(v["price"]) if obs.disponible else None
    img = d.get("featured_image") or ""
    obs.imagen = ("https:" + img) if img.startswith("//") else (img or None)
    return obs


# --------------------------------------------------------------------- todas
def leer(t: Tienda, ean: str | None = None) -> Observacion:
    if t.lector == "amazon_ficha":
        return amazon.leer(t.clave)
    if t.lector == "jsonld":
        return analizar_jsonld(_descargar(t.url), t, ean)
    if t.lector == "shopify":
        return analizar_shopify(_descargar(_url_shopify(t.url), "application/json"), t, ean)
    raise ErrorTienda("Lector desconocido: %s" % t.lector)
