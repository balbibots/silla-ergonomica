"""Datos comunes. Los precios se guardan SIEMPRE en centimos de euro (int)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Condicion de la oferta. El historico de "nuevo" nunca se mezcla con las demas.
NUEVO, USADO, REACONDICIONADO, DESCONOCIDA = "nuevo", "usado", "reacondicionado", "desconocida"

# Tipo de precio: el de la oferta destacada de la ficha (la que se compra con
# "Anadir a la cesta"), sin cupones ni envio.
OFERTA_DESTACADA = "oferta_destacada"


@dataclass
class Observacion:
    """Lo que se ha leido de una fuente en un momento dado."""

    clave: str                      # ASIN, o "shopify:<variante>"
    fecha: datetime                 # UTC
    fuente: str                     # "amazon_ficha", "shopify", ...
    condicion: str = NUEVO
    disponible: bool = True         # hay oferta NUEVA comprable
    precio_cent: int | None = None  # precio de la oferta nueva, sin cupon
    tipo_precio: str = OFERTA_DESTACADA
    cupon_cent: int | None = None   # descuento del cupon (hay que marcarlo)
    cupon_texto: str | None = None  # "45,00 €" o "10 %"
    precio_otra_condicion_cent: int | None = None  # p. ej. el de 2a mano
    otra_condicion: str | None = None
    vendedor: str | None = None
    titulo: str | None = None
    imagen: str | None = None
    valoracion: float | None = None
    num_valoraciones: int | None = None
    categoria: str | None = None

    @property
    def precio_con_cupon_cent(self) -> int | None:
        if self.precio_cent is None:
            return None
        return self.precio_cent - (self.cupon_cent or 0)


@dataclass
class Tienda:
    """Donde se vende un producto y como se lee su precio."""

    nombre: str            # "Amazon", "MediaMarkt", "Powerplanet", "Dreame"...
    lector: str            # "amazon_ficha", "jsonld" o "shopify"
    clave: str             # ASIN en Amazon; "<tienda>:<referencia>" en el resto
    url: str               # la ficha donde se compra
    extra: dict = field(default_factory=dict)  # p. ej. variant_id de Shopify


@dataclass
class Producto:
    """Un producto que se sigue, en una o varias tiendas. El precio que cuenta
    es el mejor con stock entre todas las tiendas que se pueden leer."""

    clave: str             # id del producto (en los antiguos, el ASIN)
    nombre: str
    categoria: str = "Otros"
    precio_objetivo_cent: int | None = None
    bajada_pct: float | None = None   # None = la general de la configuracion
    bajada_eur: float | None = None
    ean: str | None = None
    tiendas: list[Tienda] = field(default_factory=list)
    enlaces: list[tuple[str, str, str]] = field(default_factory=list)  # (tienda, url, nota) solo para mirar

    @property
    def url(self) -> str:
        return self.tiendas[0].url if self.tiendas else ""


# --------------------------------------------------------------------------
# Utilidades

_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_EN_URL = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)


def asin_desde(texto: str) -> str:
    """ASIN a partir de un ASIN o de una URL de Amazon Espana. Error si no vale."""
    t = texto.strip()
    if _ASIN.match(t.upper()) and not t.lower().startswith("http"):
        return t.upper()
    if not re.match(r"^https?://(www\.)?amazon\.es/", t, re.I):
        raise ValueError("No es un ASIN ni una URL de amazon.es: %r" % texto)
    m = _ASIN_EN_URL.search(t)
    if not m:
        raise ValueError("No encuentro el ASIN en la URL: %r" % texto)
    return m.group(1).upper()


def url_amazon(asin: str) -> str:
    return "https://www.amazon.es/dp/%s" % asin


def a_centimos(valor: float | str) -> int:
    """149.99 -> 14999 ; '1.299,50' -> 129950 (formato espanol)."""
    if isinstance(valor, str):
        valor = float(valor.strip().replace(".", "").replace(",", "."))
    return int(round(valor * 100))


def eur(cent: int) -> str:
    """129950 -> '1.299,50 €'."""
    signo = "−" if cent < 0 else ""
    e, c = divmod(abs(cent), 100)
    return "%s%s,%02d €" % (signo, "{:,}".format(e).replace(",", "."), c)


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ultimo_domingo(anio: int, mes: int) -> datetime:
    d = datetime(anio, mes + 1, 1, tzinfo=timezone.utc) - timedelta(days=1) if mes < 12 else \
        datetime(anio, 12, 31, tzinfo=timezone.utc)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def hora_madrid(fecha: datetime) -> datetime:
    """Hora peninsular (CET/CEST) sin depender de tzdata: el horario de verano
    europeo va del ultimo domingo de marzo al ultimo de octubre, a la 01:00 UTC."""
    fecha = fecha.astimezone(timezone.utc)
    inicio = _ultimo_domingo(fecha.year, 3).replace(hour=1)
    fin = _ultimo_domingo(fecha.year, 10).replace(hour=1)
    desfase = 2 if inicio <= fecha < fin else 1
    return (fecha + timedelta(hours=desfase)).replace(tzinfo=timezone(timedelta(hours=desfase)))


def fecha_corta(fecha: datetime) -> str:
    """'21/11/2026 16:30' en hora de Madrid."""
    return hora_madrid(fecha).strftime("%d/%m/%Y %H:%M")


def iso(fecha: datetime) -> str:
    return fecha.astimezone(timezone.utc).isoformat(timespec="seconds")


def desde_iso(texto: str) -> datetime:
    f = datetime.fromisoformat(texto)
    return f if f.tzinfo else f.replace(tzinfo=timezone.utc)
