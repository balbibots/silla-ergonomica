"""Fuentes de ofertas para el modo descubrimiento.

CholloMazon (https://chollomazon.com/datos) publica sus ofertas activas de
Amazon Espana en un CSV con licencia CC BY 4.0 ("puedes usarlos citando a
CholloMazon con un enlace a esta pagina"). Su robots.txt permite /datos/. El
CSV se sirve con Cache-Control max-age=3600, asi que no se pide mas de una vez
por hora, con peticion condicional (ETag / Last-Modified) y respetando
Retry-After. Todas las tarjetas que salen de aqui llevan la atribucion.
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from .modelo import NUEVO, a_centimos, desde_iso, url_amazon

UA = "silla-sihoo-bot/2.0 (bot personal; +https://github.com/balbibots/silla-ergonomica)"
CHOLLOMAZON_CSV = "https://chollomazon.com/datos/ofertas.csv"
CHOLLOMAZON_ATRIBUCION = "Datos: CholloMazon (CC BY 4.0) · chollomazon.com/datos"


@dataclass
class Candidato:
    asin: str
    titulo: str
    precio_cent: int
    categoria: str
    fuente: str
    condicion: str = NUEVO
    disponible: bool = True
    precio_referencia_cent: int | None = None   # el que muestra Amazon como referencia
    cupon: str | None = None
    marca: str | None = None
    publicada: datetime | None = None
    comprobada: datetime | None = None
    enlace_fuente: str | None = None
    valoracion: float | None = None
    num_valoraciones: int | None = None
    imagen: str | None = None
    vendedor: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return url_amazon(self.asin)


class SinNovedades(Exception):
    """La fuente no ha cambiado desde la ultima vez (HTTP 304)."""


class FuenteNoDisponible(RuntimeError):
    def __init__(self, mensaje: str, reintentar_tras: timedelta | None = None):
        super().__init__(mensaje)
        self.reintentar_tras = reintentar_tras


def _fecha(texto: str) -> datetime | None:
    return desde_iso(texto.replace("Z", "+00:00")) if texto else None


def parsear_chollomazon(texto: str) -> list[Candidato]:
    lista = []
    for f in csv.DictReader(io.StringIO(texto)):
        try:
            precio = a_centimos(float(f["precio_eur"]))
        except (KeyError, ValueError):
            continue
        asin = (f.get("asin") or "").strip().upper()
        if len(asin) != 10 or precio <= 0:
            continue
        ref = f.get("precio_referencia_eur") or ""
        marca = (f.get("marca") or "").strip()
        if marca.lower().startswith("marca:"):
            marca = marca[6:].strip()
        lista.append(Candidato(
            asin=asin, titulo=(f.get("titulo") or "").strip(), precio_cent=precio,
            categoria=(f.get("categoria") or "Otros").strip(), fuente="chollomazon",
            precio_referencia_cent=a_centimos(float(ref)) if ref else None,
            cupon=(f.get("cupon") or "").strip() or None, marca=marca or None,
            publicada=_fecha(f.get("publicada", "")), comprobada=_fecha(f.get("comprobada", "")),
            enlace_fuente=(f.get("url") or "").strip() or None))
    return lista


class CholloMazon:
    nombre = "chollomazon"
    atribucion = CHOLLOMAZON_ATRIBUCION

    def __init__(self, almacen, descargar=None):
        self.almacen = almacen
        self.descargar = descargar or self._descargar

    def _descargar(self) -> tuple[int, str, dict]:
        cab = {"User-Agent": UA, "Accept": "text/csv"}
        etag = self.almacen.meta("chollomazon_etag")
        modificado = self.almacen.meta("chollomazon_last_modified")
        if etag:
            cab["If-None-Match"] = etag
        if modificado:
            cab["If-Modified-Since"] = modificado
        req = urllib.request.Request(CHOLLOMAZON_CSV, headers=cab)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8"), dict(r.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, "", dict(exc.headers)

    def candidatos(self) -> list[Candidato]:
        estado, texto, cab = self.descargar()
        if estado == 304:
            # Sin cambios: se revisa la ultima version guardada, para que las
            # ofertas que no cupieron en el limite de avisos no se queden atras.
            guardado = self.almacen.meta("chollomazon_csv")
            if not guardado:
                raise SinNovedades()
            return parsear_chollomazon(guardado)
        if estado in (429, 503):
            try:
                espera = timedelta(seconds=float(cab.get("Retry-After", "3600")))
            except ValueError:
                espera = timedelta(hours=1)
            raise FuenteNoDisponible("CholloMazon responde HTTP %d" % estado, espera)
        if estado != 200:
            raise FuenteNoDisponible("CholloMazon responde HTTP %d" % estado)
        if not texto.startswith("titulo,asin"):
            raise FuenteNoDisponible("El CSV de CholloMazon ha cambiado de formato")
        for nombre, clave in (("ETag", "chollomazon_etag"), ("Last-Modified", "chollomazon_last_modified")):
            if cab.get(nombre):
                self.almacen.poner_meta(clave, cab[nombre])
        self.almacen.poner_meta("chollomazon_csv", texto)
        return parsear_chollomazon(texto)


# ----------------------------------------------------------------- Chollometro
CHOLLOMETRO_RSS = "https://www.chollometro.com/rss/nuevos"
CHOLLOMETRO_ATRIBUCION = "Publicado en Chollometro (comunidad) · precio sin verificar"
_PRECIO = r"(\d{1,5}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s?€"
_ANTES = re.compile(r"(?:antes|pvpr?|precio (?:habitual|anterior|original|recomendado)|"
                    r"precio de venta recomendado|en lugar de|baja de)\D{0,25}?" + _PRECIO, re.I)
_TACHADO = re.compile(r"<(?:s|del|strike)>\s*" + _PRECIO, re.I)


@dataclass
class Chollo:
    """Una oferta publicada por la comunidad de Chollometro."""

    id: str
    titulo: str
    tienda: str
    precio_cent: int | None
    url: str
    publicada: datetime | None
    texto: str                    # titulo + descripcion, sin HTML
    imagen: str | None = None
    categoria: str | None = None
    precio_antes_cent: int | None = None   # solo si el texto lo da


def _precio_texto(t: str) -> int | None:
    t = t.strip()
    if re.search(r"[.,]\d{3}(?:[.,]|$)", t) and "," in t:      # 1.299,99
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    try:
        return a_centimos(float(t))
    except ValueError:
        return None


def parsear_chollometro(xml: str) -> list[Chollo]:
    out = []
    for it in xml.split("<item>")[1:]:
        def campo(etiqueta):
            m = re.search(r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (etiqueta, etiqueta), it, re.S)
            return m.group(1).strip() if m else ""
        enlace = campo("link")
        m = re.search(r"-(\d+)$", enlace)
        if not enlace or not m:
            continue
        mer = re.search(r'<pepper:merchant name="([^"]*)" price="([^"]*)"', it)
        desc_html = campo("description")
        texto = html.unescape(re.sub(r"<[^>]+>", " ", campo("title") + " " + desc_html))
        texto = re.sub(r"\s+", " ", texto)
        precio = _precio_texto(mer.group(2).replace("€", "")) if mer and mer.group(2) else None
        antes = None
        for patron, fuente in ((_TACHADO, desc_html), (_ANTES, texto)):
            ma = patron.search(fuente)
            if ma:
                antes = _precio_texto(ma.group(1))
                break
        grande = re.search(r'src="(https://static\.chollometro\.com/threads/raw/[^"]+/fs/[^"]+)"', desc_html)
        peque = re.search(r'<media:content[^>]*url="([^"]+)"', it)
        try:
            publicada = parsedate_to_datetime(campo("pubDate"))
        except (TypeError, ValueError):
            publicada = None
        out.append(Chollo(id=m.group(1), titulo=html.unescape(campo("title")),
                          tienda=html.unescape(mer.group(1)) if mer else "", precio_cent=precio,
                          url=enlace, publicada=publicada, texto=texto,
                          imagen=(grande or peque).group(1) if (grande or peque) else None,
                          categoria=html.unescape(campo("category")) or None,
                          precio_antes_cent=antes if antes and precio and antes > precio else None))
    return out


class Chollometro:
    """RSS publico de chollos nuevos. Se pide como mucho cada pocos minutos."""

    nombre = "chollometro"
    atribucion = CHOLLOMETRO_ATRIBUCION
    comunidad = True

    def __init__(self, descargar=None):
        self.descargar = descargar or self._descargar

    @staticmethod
    def _descargar() -> tuple[int, str, dict]:
        req = urllib.request.Request(CHOLLOMETRO_RSS, headers={"User-Agent": UA, "Accept": "application/rss+xml"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, "", dict(exc.headers)

    def candidatos(self) -> list[Chollo]:
        estado, texto, cab = self.descargar()
        if estado in (429, 503):
            try:
                espera = timedelta(seconds=float(cab.get("Retry-After", "900")))
            except ValueError:
                espera = timedelta(minutes=15)
            raise FuenteNoDisponible("Chollometro responde HTTP %d" % estado, espera)
        if estado != 200 or "<rss" not in texto[:500]:
            raise FuenteNoDisponible("Chollometro responde HTTP %d" % estado)
        return parsear_chollometro(texto)


class Simulada:
    """Candidatos fijos desde un JSON. Solo para pruebas."""

    nombre = "simulado"
    atribucion = "Datos: fuente simulada (pruebas)"

    def __init__(self, ruta: str | Path):
        self.datos = json.loads(Path(ruta).read_text(encoding="utf-8"))

    def candidatos(self) -> list[Candidato]:
        out = []
        for x in self.datos["candidatos"]:
            x = dict(x)
            for k in ("publicada", "comprobada"):
                if x.get(k):
                    x[k] = desde_iso(x[k])
            out.append(Candidato(fuente=self.nombre, **x))
        return out
