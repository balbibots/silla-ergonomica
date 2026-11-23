"""Precios e historicos de tiendas publicados por webs de seguimiento de terceros.

Se usan para lo que el bot no puede leer directamente, o para tener un
historico anterior a su primera lectura. Ambas se leen con peticiones HTTP
normales, identificadas como el bot, a paginas publicas que su robots.txt
permite (comprobado el 21/11/2026; solo vetan /api/, /admin/ y redirecciones):

- pccomtracker.com: la ficha de cada producto trae en JSON-LD el precio y el
  stock actuales de PcComponentes (con la URL exacta de la ficha de
  PcComponentes y la hora de su ultima comprobacion), y en su FAQ el minimo
  del ultimo ano con su fecha y el "precio habitual". No tiene buscador
  publico permitido, asi que su URL se pone una vez en la configuracion.
- reacofertas.es: para una URL de PcComponentes, MediaMarkt o Powerplanet,
  /tracking-precios?q=<url> devuelve la ficha con la serie de precios de los
  ultimos 180 dias (los dias en que cambio), incrustada en el HTML del grafico.

Ninguna de las dos es la tienda: el precio puede llevar horas de retraso. Por
eso un dato de mas de MAX_HORAS se descarta, la tarjeta dice de donde sale, y
una serie que no cuadra con la lectura actual de la tienda no se guarda.
"""
from __future__ import annotations

import gzip
import html as htmlmod
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from .fuentes import UA
from .modelo import Observacion, Tienda, a_centimos, ahora_utc

MAX_HORAS = 26
PCCOMTRACKER = "pccomtracker"
REACOFERTAS = "reacofertas"
REACO_TIENDAS = ("pccomponentes.com", "mediamarkt.es", "powerplanetonline.com")
MESES = {m: i for i, m in enumerate(("enero febrero marzo abril mayo junio julio agosto septiembre "
                                       "octubre noviembre diciembre").split(), 1)}


class ErrorTercero(RuntimeError):
    pass


def descargar(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html",
                                               "Accept-Language": "es-ES,es;q=0.9", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            datos = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                datos = gzip.decompress(datos)
            return datos.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise ErrorTercero("%s responde HTTP %d" % (urlsplit(url).netloc, exc.code)) from exc


def _misma_ficha(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return pa.netloc.lower().removeprefix("www.") == pb.netloc.lower().removeprefix("www.") \
        and pa.path.rstrip("/") == pb.path.rstrip("/")


def _jsonld(html: str):
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            yield json.loads(m.group(1))
        except json.JSONDecodeError:
            continue


# ---------------------------------------------------------------- pccomtracker
def analizar_pccomtracker(html: str, t: Tienda, ahora: datetime | None = None) -> Observacion:
    """Precio actual de la tienda t (su URL) segun la ficha de pccomtracker."""
    ahora = ahora or ahora_utc()
    prod = next((d for d in _jsonld(html) if d.get("@type") == "Product"), None)
    if not prod:
        raise ErrorTercero("la ficha de pccomtracker no trae datos de producto")
    ofertas = prod.get("offers") or {}
    ofertas = ofertas.get("offers", [ofertas]) if isinstance(ofertas, dict) else ofertas
    oferta = next((o for o in ofertas if _misma_ficha(o.get("url", ""), t.url)), None)
    if not oferta:
        raise ErrorTercero("la ficha de pccomtracker no es la de %s" % t.url)
    try:
        modificado = datetime.fromisoformat(prod["dateModified"].replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ErrorTercero("pccomtracker no dice cuando comprobo el precio") from exc
    if ahora - modificado > timedelta(hours=MAX_HORAS):
        raise ErrorTercero("el dato de pccomtracker tiene %d h; no se usa"
                           % ((ahora - modificado).total_seconds() // 3600))
    disponible = str(oferta.get("availability", "")).endswith(("InStock", "LimitedAvailability"))
    # La fecha es la de esta lectura (el dato tiene como mucho MAX_HORAS).
    obs = Observacion(clave=t.clave, fecha=ahora, fuente=PCCOMTRACKER, disponible=disponible,
                      titulo=prod.get("name"), vendedor=oferta.get("name") or t.nombre)
    img = prod.get("image")
    obs.imagen = img if isinstance(img, str) else None
    if disponible and oferta.get("priceCurrency", "EUR") == "EUR":
        obs.precio_cent = a_centimos(float(oferta["price"]))
    elif disponible:
        raise ErrorTercero("precio en otra moneda")
    return obs


def _fecha_es(texto: str) -> datetime | None:
    m = re.search(r"(\d{1,2}) de (\w+) de (\d{4})", texto)
    if not m or m.group(2).lower() not in MESES:
        return None
    return datetime(int(m.group(3)), MESES[m.group(2).lower()], int(m.group(1)), tzinfo=timezone.utc)


def _euros(texto: str) -> int | None:
    m = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€", texto)
    return a_centimos(float(m.group(1).replace(".", "").replace(",", "."))) if m else None


def resumen_pccomtracker(html: str) -> dict:
    """Del FAQ de la ficha: minimo del ultimo ano (con fecha) y precio habitual."""
    out = {}
    for d in _jsonld(html):
        if d.get("@type") != "FAQPage":
            continue
        for q in d.get("mainEntity", []):
            pregunta = q.get("name", "").lower()
            texto = htmlmod.unescape(q.get("acceptedAnswer", {}).get("text", ""))
            if "más bajo" in pregunta or "mas bajo" in pregunta:
                out["minimo_cent"], out["minimo_fecha"] = _euros(texto), _fecha_es(texto)
            m = re.search(r"precio habitual \(([^)]*)\)", texto)
            if m:
                out["habitual_cent"] = _euros(m.group(1))
    return out


def leer_pccomtracker(t: Tienda) -> Observacion:
    return analizar_pccomtracker(descargar(t.extra[PCCOMTRACKER]), t)


# ----------------------------------------------------------------- reacofertas
def url_reacofertas(url_tienda: str) -> str:
    return "https://reacofertas.es/tracking-precios?q=" + quote(url_tienda, safe="")


def serie_reacofertas(html: str) -> list[tuple[datetime, int]]:
    m = re.search(r"const rawPoints = (\[.*?\]);", html)
    if not m:
        raise ErrorTercero("reacofertas no tiene historico de esa ficha")
    puntos = []
    for p in json.loads(m.group(1)):
        fecha = datetime.strptime(p["fecha"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if p.get("precio"):
            puntos.append((fecha, a_centimos(float(p["precio"]))))
    if not puntos:
        raise ErrorTercero("la serie de reacofertas esta vacia")
    return puntos


def cubre_reacofertas(url: str) -> bool:
    return urlsplit(url).netloc.lower().removeprefix("www.").endswith(REACO_TIENDAS)


def cuadra(puntos: list[tuple[datetime, int]], precio_actual: int | None, tolerancia: float = 0.03) -> bool:
    """La serie es de este producto si su ultimo precio coincide con el que el
    bot acaba de leer en la tienda (o no hay lectura con la que comparar)."""
    if precio_actual is None:
        return True
    return abs(puntos[-1][1] - precio_actual) <= max(100, precio_actual * tolerancia)
