"""Descubrimiento con precios de referencia REALES: pccomtracker y reacofertas.

Las dos webs siguen a diario el precio de miles de productos de tiendas
espanolas y publican, en paginas que su robots.txt permite, la lista de
bajadas y la ficha de cada producto con su historia. A diferencia de un precio
tachado o un PVPR, su referencia sale de precios que se han cobrado de verdad:

- pccomtracker (PcComponentes): portada ("Mayores bajadas de hoy", "Mejores
  ofertas"), las categorias de tus intereses y, de cada ficha, el precio
  actual con la hora de su comprobacion, el "precio habitual" y el minimo del
  ultimo ano.
- reacofertas (PcComponentes, MediaMarkt, Powerplanet): la lista de bajadas
  recientes, sus busquedas (?q=) para tus intereses y, de cada ficha, la serie
  de 180 dias. La referencia es la MEDIANA DIARIA de los ultimos 90 dias (cada
  dia cuenta con el precio que tenia), que no se deja enganar por un pico de
  un dia.

Se leen pocas fichas por pasada (max_fichas) y cada ficha se guarda 6 h, asi
que el trafico es de unas decenas de paginas cada hora o cada pocas horas.
Ninguna de las dos es la tienda: el precio lo comprueban ellas, no el bot, y
la tarjeta lo dice. Sus enlaces de compra son de afiliado, asi que el boton
lleva a la ficha de la tienda (pccomtracker trae su URL) o a la de reacofertas.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timedelta
from urllib.parse import quote

from . import terceros
from .almacen import Almacen
from .fuentes import Chollo, FuenteNoDisponible
from .modelo import a_centimos, ahora_utc, desde_iso, iso

PCT = "https://pccomtracker.com"
REACO = "https://reacofertas.es/tracking-precios"
CACHE_FICHA = timedelta(hours=6)
PCT_ATRIBUCION = "pccomtracker.com"
REACO_ATRIBUCION = "reacofertas.es"


def _limpio(s: str) -> str:
    import html as h
    return re.sub(r"\s+", " ", h.unescape(re.sub(r"<!--.*?-->|<[^>]+>", " ", s))).strip()


def _euros(s: str) -> int | None:
    m = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€", _limpio(s))
    return a_centimos(float(m.group(1).replace(".", "").replace(",", "."))) if m else None


# ----------------------------------------------------------------- pccomtracker
def tarjetas_pccomtracker(html: str) -> list[dict]:
    """Tarjetas de producto de una pagina de pccomtracker (portada o categoria)."""
    out = []
    for m in re.finditer(r'<a class="group[^"]*" href="(/producto/[^"]+)">(.*?)</a>', html, re.S):
        cuerpo = m.group(2)
        titulo = re.search(r"<h3[^>]*>(.*?)</h3>", cuerpo, re.S)
        precio = re.search(r'<span class="text-(?:2xl|xl) font-bold[^"]*">(.*?)</span>', cuerpo, re.S)
        antes = re.search(r'<span class="[^"]*line-through[^"]*">(.*?)</span>', cuerpo, re.S)
        cat = re.search(r'<p class="text-\[13px\][^"]*">(.*?)</p>', cuerpo, re.S)
        if not titulo or not precio:
            continue
        out.append({"href": m.group(1), "titulo": _limpio(titulo.group(1)), "precio": _euros(precio.group(1)),
                    "antes": _euros(antes.group(1)) if antes else None,
                    "categoria": _limpio(cat.group(1)) if cat else None})
    return out


def ficha_pccomtracker(html: str) -> dict | None:
    """Precio de PcComponentes, stock, hora de comprobacion y referencias."""
    prod = next((d for d in terceros._jsonld(html) if d.get("@type") == "Product"), None)
    if not prod:
        return None
    ofertas = prod.get("offers") or {}
    ofertas = ofertas.get("offers", [ofertas]) if isinstance(ofertas, dict) else ofertas
    of = next((o for o in ofertas if "pccomponentes" in o.get("url", "")), None)
    if not of:
        return None
    try:
        modificado = datetime.fromisoformat(prod["dateModified"].replace("Z", "+00:00"))
        precio = a_centimos(float(of["price"]))
    except (KeyError, ValueError):
        return None
    r = terceros.resumen_pccomtracker(html)
    img = prod.get("image")
    return {"titulo": prod.get("name"), "url": of["url"], "precio": precio, "modificado": modificado,
            "disponible": str(of.get("availability", "")).endswith("InStock"),
            "imagen": img if isinstance(img, str) else None, "habitual": r.get("habitual_cent"),
            "minimo": r.get("minimo_cent"), "minimo_fecha": r.get("minimo_fecha")}


# ------------------------------------------------------------------ reacofertas
def tarjetas_reacofertas(html: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<h3[^>]*>(.*?)</h3>(.*?)href="/tracking-precios\?id=(\d+)"', html, re.S):
        bloque = m.group(2)
        precio = re.search(r'fw-bold[^"]*">\s*([\d.,]+\s*€)', bloque)
        antes = re.search(r'line-through[^"]*">\s*([\d.,]+\s*€)', bloque)
        if not precio:
            continue
        out.append({"id": m.group(3), "titulo": _limpio(m.group(1)), "precio": _euros(precio.group(1)),
                    "antes": _euros(antes.group(1)) if antes else None})
    return out


def mediana_diaria(puntos: list[tuple[datetime, int]], hasta: datetime, dias: int = 90) -> tuple[int, int] | None:
    """(mediana, dias cubiertos): el precio de cada dia de los ultimos 'dias'
    ANTERIORES a 'hasta' (cada cambio vale hasta el siguiente)."""
    if not puntos:
        return None
    fin = hasta.date()
    inicio = max(puntos[0][0].date(), fin - timedelta(days=dias))
    diarios, i, actual = [], 0, None
    d = inicio
    while d < fin:
        while i < len(puntos) and puntos[i][0].date() <= d:
            actual = puntos[i][1]
            i += 1
        if actual is not None:
            diarios.append(actual)
        d += timedelta(days=1)
    if not diarios:
        return None
    return int(statistics.median(diarios)), len(diarios)


TIENDAS = {"pccomponentes": "PcComponentes", "mediamarkt": "MediaMarkt", "powerplanet": "Powerplanet"}


def _tienda(texto: str) -> str:
    t = re.sub(r"\W+", "", _limpio(texto).lower())
    return next((v for k, v in TIENDAS.items() if k in t), _limpio(texto))


def ficha_reacofertas(html: str) -> dict | None:
    try:
        puntos = terceros.serie_reacofertas(html)
    except terceros.ErrorTercero:
        return None
    titulo = re.search(r"<title>Histórico de precios: (.*?) \| REACOFERTAS", html)
    tienda = re.search(r"Ver oferta en ([^<]+?)\s*<", html)
    img = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    actual = re.search(r'Precio Actual</div>\s*<div class="stat-val">([^<]+)</div>', html)
    return {"titulo": _limpio(titulo.group(1)) if titulo else None, "puntos": puntos,
            "tienda": _tienda(tienda.group(1)) if tienda else None,
            "imagen": img.group(1) if img else None,
            "precio": _euros(actual.group(1)) if actual else puntos[-1][1]}


# ---------------------------------------------------------------- las fuentes
class _ConFichas:
    """Lee pocas fichas por pasada y las recuerda unas horas."""

    nombre = ""
    comunidad = True

    def __init__(self, cfg: dict, almacen: Almacen, descargar=None, ahora=None):
        self.cfg, self.almacen = cfg, almacen
        self.descargar = descargar or terceros.descargar
        self.ahora = ahora

    @property
    def ajustes(self) -> dict:
        return self.cfg["descubrimiento"]["fuentes"][self.nombre]

    def _ficha(self, url: str, precio_lista: int | None, analizar):
        clave = "ficha:%s" % url
        ahora = self.ahora or ahora_utc()
        guardada = self.almacen.meta(clave)
        if guardada:
            g = json.loads(guardada)
            if g["precio_lista"] == precio_lista and ahora - desde_iso(g["fecha"]) < CACHE_FICHA:
                return g["html"] and analizar(g["html"])
        if self.leidas >= self.ajustes["max_fichas"]:
            return None
        self.leidas += 1
        try:
            html = self.descargar(url)
        except terceros.ErrorTercero as exc:
            print("  %s: %s" % (self.nombre, exc))
            html = ""
        self.almacen.poner_meta(clave, json.dumps({"precio_lista": precio_lista, "fecha": iso(ahora),
                                                   "html": _recorte(html)}))
        return html and analizar(html)

    def _intereses(self, clave: str) -> list[tuple[str, dict]]:
        return [(x, i) for i in self.cfg["descubrimiento"].get("intereses", []) if i.get("activo", True)
                for x in i.get(clave, [])]


def _recorte(html: str) -> str:
    """Solo lo que hace falta de la ficha (JSON-LD, serie, titulo...)."""
    partes = re.findall(r'<script[^>]*application/ld\+json[^>]*>.*?</script>|const rawPoints = \[.*?\];|'
                        r'<title>.*?</title>|<meta property="og:image"[^>]*>|Ver oferta en [^<]+<|'
                        r'Precio Actual</div>\s*<div class="stat-val">[^<]+</div>', html, re.S)
    return "\n".join(partes)


def _encaja(titulo: str, precio: int | None, interes: dict) -> bool:
    t = titulo.lower()
    if precio is None or not any(k.lower() in t for k in interes["incluir"]):
        return False
    if any(k.lower() in t for k in interes.get("excluir", [])):
        return False
    tope = interes.get("precio_objetivo") or interes.get("precio_max")
    return (interes.get("precio_min") or 0) * 100 <= precio <= (tope or 10 ** 6) * 100 * 1.25


class PCComTracker(_ConFichas):
    nombre = "pccomtracker"
    atribucion = PCT_ATRIBUCION

    def candidatos(self) -> list[Chollo]:
        self.leidas = 0
        ahora = self.ahora or ahora_utc()
        try:
            portada = tarjetas_pccomtracker(self.descargar(PCT + "/"))
        except terceros.ErrorTercero as exc:
            raise FuenteNoDisponible(str(exc)) from exc
        lista = [(x, True) for x in portada]
        for cat, interes in self._intereses("pccomtracker_categorias"):
            try:
                for x in tarjetas_pccomtracker(self.descargar("%s/categoria/%s" % (PCT, cat))):
                    if _encaja(x["titulo"], x["precio"], interes):
                        lista.append((x, False))
            except terceros.ErrorTercero as exc:
                print("  pccomtracker: categoria %s no disponible: %s" % (cat, exc))
        out, vistos = [], set()
        for x, oferta in lista:
            if x["href"] in vistos:
                continue
            vistos.add(x["href"])
            f = self._ficha(PCT + x["href"], x["precio"], ficha_pccomtracker)
            if not f or not f["disponible"] or ahora - f["modificado"] > timedelta(hours=terceros.MAX_HORAS):
                continue
            out.append(Chollo(
                id=x["href"].rsplit("-", 1)[-1], titulo=f["titulo"] or x["titulo"], tienda="PcComponentes",
                precio_cent=f["precio"], url=f["url"], publicada=f["modificado"], texto=x["titulo"],
                imagen=f["imagen"], categoria=x["categoria"], fuente=self.nombre, es_oferta=oferta,
                precio_antes_cent=x["antes"] if x["antes"] and x["antes"] > f["precio"] else None,
                ref_hist_cent=f["habitual"], ref_texto="precio habitual según pccomtracker",
                minimo_cent=f["minimo"], minimo_fecha=f["minimo_fecha"], minimo_texto="del año",
                pagina=PCT + x["href"]))
        print("  pccomtracker: %d tarjetas, %d fichas leidas" % (len(vistos), self.leidas))
        return out


class Reacofertas(_ConFichas):
    nombre = "reacofertas"
    atribucion = REACO_ATRIBUCION

    def candidatos(self) -> list[Chollo]:
        self.leidas = 0
        ahora = self.ahora or ahora_utc()
        try:
            lista = [(x, True) for x in tarjetas_reacofertas(self.descargar(REACO))]
        except terceros.ErrorTercero as exc:
            raise FuenteNoDisponible(str(exc)) from exc
        for q, interes in self._intereses("reacofertas_busquedas"):
            try:
                for x in tarjetas_reacofertas(self.descargar(REACO + "?q=" + quote(q))):
                    if _encaja(x["titulo"], x["precio"], interes):
                        lista.append((x, False))
            except terceros.ErrorTercero as exc:
                print("  reacofertas: busqueda %r no disponible: %s" % (q, exc))
        out, vistos = [], set()
        for x, oferta in lista:
            if x["id"] in vistos:
                continue
            vistos.add(x["id"])
            pagina = "%s?id=%s" % (REACO, x["id"])
            f = self._ficha(pagina, x["precio"], ficha_reacofertas)
            if not f:
                continue
            puntos = f["puntos"]
            med = mediana_diaria(puntos, ahora)
            ref = med[0] if med and med[1] >= 30 else None
            ventana = [(d, p) for d, p in puntos if d.date() < ahora.date() and d >= ahora - timedelta(days=180)]
            minimo = min(ventana, key=lambda v: (v[1], v[0])) if ventana else None
            out.append(Chollo(
                id=x["id"], titulo=f["titulo"] or x["titulo"], tienda=f["tienda"] or "",
                precio_cent=f["precio"], url=pagina, publicada=ahora, texto=x["titulo"], imagen=f["imagen"],
                categoria=None, fuente=self.nombre, es_oferta=oferta,
                precio_antes_cent=x["antes"] if x["antes"] and x["antes"] > f["precio"] else None,
                ref_hist_cent=ref, ref_texto="mediana de 90 días según reacofertas",
                minimo_cent=minimo[1] if minimo else None, minimo_fecha=minimo[0] if minimo else None,
                minimo_texto="de 6 meses", pagina=pagina))
        print("  reacofertas: %d tarjetas, %d fichas leidas" % (len(vistos), self.leidas))
        return out

