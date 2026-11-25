"""Fuentes directas: las propias tiendas, sin esperar a que nadie publique la oferta.

Comprobado el 24/11/2026 con una peticion normal identificada como el bot:

| Tienda         | Resultado | Uso |
|----------------|-----------|-----|
| Amazon /deals  | 200, sin CAPTCHA | Lista de ofertas activas (30 por carga) en un JSON de la pagina: ASIN, titulo, descuento ANUNCIADO, tipo y estado. Sin precio: se lee la ficha de cada finalista, que trae el precio, el minimo de 30 dias y el precio mediano o el PVPR |
| PcComponentes  | 403 (Cloudflare) | No. Via pccomtracker, Chollometro + juez, o el feed de Awin |
| MediaMarkt     | 200 | Paginas de campana (Ofertas Flash, Ofertas del dia, Black Friday...): el HTML trae los primeros productos de cada bloque con EAN, precio, tachado y su tipo (LOP = minimo de 30 dias, real; XOP = precio de referencia/PVPR), stock y envio. Lo demas carga por su API interna (GraphQL): no se usa |
| El Corte Ingles, Fnac | 403 incluso al robots.txt | No |
| Carrefour, Worten | 403 | No |
| Miravia        | 200 | Portada con recomendaciones personalizadas y descuentos anunciados por vendedores; fichas de producto prohibidas en parte por robots.txt. Pendiente |
| AliExpress     | 200 | Paginas con JavaScript; la via prevista es su API de afiliados (registro gratuito). Pendiente |

Amazon /deals no esta prohibida en su robots.txt para agentes genericos. Se
pide como mucho cada 15 minutos (10 en las ventanas rapidas) y un CAPTCHA se
trata como "no disponible" (se reintenta mas tarde, nunca se resuelve).
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from urllib.parse import quote

import check_price as cp

from . import terceros
from . import fuentes_historico as fh
from .fuentes import Candidato, Chollo, FuenteNoDisponible
from .modelo import a_centimos, ahora_utc, desde_iso, iso

AMAZON_DEALS = "https://www.amazon.es/deals"


def ofertas_amazon(html: str) -> list[dict]:
    """Las ofertas del JSON 'productSearchResponse' de la pagina de ofertas."""
    i = html.find('"productSearchResponse"')
    if i < 0:
        return []
    try:
        datos, _ = json.JSONDecoder().raw_decode(html[html.find("{", i):])
    except ValueError:
        return []
    out = []
    for p in datos.get("products", []):
        texto = "".join(f.get("text", "") for f in ((p.get("dealBadge") or {}).get("label") or {})
                        .get("content", {}).get("fragments", []))
        m = re.search(r"(\d{1,2})\s*%", texto)
        detalle = p.get("dealDetails") or {}
        out.append({"asin": p.get("asin"), "titulo": p.get("title") or "",
                    "anunciado_pct": int(m.group(1)) if m else None,
                    "tipo": detalle.get("type"), "estado": detalle.get("state"), "deal": detalle.get("id"),
                    "condicion": p.get("buyingOptionType"),
                    "imagen": ((p.get("image") or {}).get("hiRes") or {}).get("baseUrl")})
    return out


class AmazonOfertas:
    """Pagina de ofertas de Amazon.es. Los candidatos no traen precio: lo pone
    la lectura de la ficha (verificacion), que es la que decide."""

    nombre = "amazon_ofertas"
    atribucion = "Amazon.es (página de ofertas y ficha)"

    def __init__(self, descargar=None):
        self.descargar = descargar or terceros.descargar

    def candidatos(self) -> list[Candidato]:
        try:
            html = self.descargar(AMAZON_DEALS)
        except terceros.ErrorTercero as exc:
            raise FuenteNoDisponible(str(exc), timedelta(minutes=30)) from exc
        if cp._es_captcha_amazon(html):
            raise FuenteNoDisponible("Amazon ha servido un CAPTCHA", timedelta(minutes=60))
        lote = []
        for o in ofertas_amazon(html):
            if not o["asin"] or o["estado"] not in (None, "AVAILABLE"):
                continue
            lote.append(Candidato(asin=o["asin"], titulo=o["titulo"], precio_cent=None, categoria="",
                                  fuente=self.nombre, condicion="nuevo" if o["condicion"] in (None, "NEW") else "otra",
                                  imagen=o["imagen"],
                                  extra={"anunciado_pct": o["anunciado_pct"], "tipo": o["tipo"], "deal": o["deal"]}))
        if not lote:
            raise FuenteNoDisponible("la pagina de ofertas no trae la lista (formato cambiado?)", timedelta(hours=2))
        return lote


# ------------------------------------------------------------------ MediaMarkt
MEDIAMARKT = "https://www.mediamarkt.es"
CAMPANAS_MM = ["ofertas-flash", "ofertas-destacadas", "black-friday", "campanas-y-ofertas"]


def _estado_mm(html: str) -> dict:
    i = html.find("__PRELOADED_STATE__")
    if i < 0:
        return {}
    j = html.find("{", i)
    k = html.find("</script>", j)
    try:
        estado = json.loads(re.sub(r":undefined([,}\]])", r":null\1", html[j:k].rstrip().rstrip(";")))
    except ValueError:
        return {}
    return estado.get("apolloState") or {}


def productos_mediamarkt(html: str) -> list[dict]:
    """Productos que la pagina trae en su estado inicial (Apollo)."""
    ap = _estado_mm(html)
    out = []
    for k, v in ap.items():
        if not k.startswith("CofrPriceFeature") or not isinstance(v, dict) or "[" in v.get("id", "["):
            continue
        pid = v["id"]
        core = ap.get("CofrCoreFeature:" + pid) or {}
        media = ap.get("CofrMediaAssetsFeature:" + pid) or {}
        online = ap.get("CofrOnlineStatusFeature:" + pid) or {}
        if not core.get("productName") or not media.get("urlRelative"):
            continue
        precio = (v.get("price") or {}).get("amount")
        promo = (v.get("promoPrice") or {}).get("amount")
        if promo and (not precio or promo < precio):
            precio = promo
        tachado = v.get("strikePrice") or {}
        migas = [ap.get(b.get("__ref"), {}).get("categoryName") for b in core.get("breadcrumbs") or []]
        resenas = core.get("reviewStatistics") or {}
        out.append({"id": pid, "titulo": core["productName"], "url": MEDIAMARKT + media["urlRelative"],
                    "ean": core.get("ean"), "precio": a_centimos(precio) if precio else None,
                    "tachado": a_centimos(tachado["amount"]) if tachado.get("amount") else None,
                    "tachado_tipo": tachado.get("type"),
                    "envio": a_centimos((v.get("price") or {}).get("shippingCost") or 0),
                    "disponible": online.get("onlineStatus") == "AVAILABLE" and online.get("isAvailableAndBuyable", True),
                    "marketplace": bool(core.get("isProductOfTypeMarketplace")),
                    "imagen": ((media.get("productMainImage") or {}).get("link")),
                    "categoria": " > ".join(m for m in migas if m) or None,
                    "valoracion": resenas.get("averageOverallRating"), "opiniones": resenas.get("totalReviewCount")})
    return out


class MediaMarktCampanas:
    """Paginas de campana de MediaMarkt (permitidas en su robots.txt). Para lo
    que trae un tachado de referencia (XOP, tipo PVPR) busca la serie real en
    reacofertas por la URL exacta de la ficha (como mucho max_consultas por
    pasada, guardada 6 h)."""

    nombre = "mediamarkt"
    atribucion = "MediaMarkt (páginas de campaña)"
    comunidad = True

    def __init__(self, cfg: dict, almacen, descargar=None, ahora=None):
        self.cfg, self.almacen = cfg, almacen
        self.descargar = descargar or terceros.descargar
        self.ahora = ahora

    def _historia(self, url: str, ahora):
        clave = "mmhist:%s" % url
        g = self.almacen.meta(clave)
        if g:
            g = json.loads(g)
            if ahora - desde_iso(g["fecha"]) < timedelta(hours=6):
                return g["dato"]
        if self.consultas >= self.cfg["descubrimiento"]["fuentes"][self.nombre].get("max_consultas", 8):
            return None
        self.consultas += 1
        dato = None
        try:
            f = fh.ficha_reacofertas(self.descargar(terceros.url_reacofertas(url)))
        except terceros.ErrorTercero:
            f = None
        if f:
            med = fh.mediana_diaria(f["puntos"], ahora)
            dato = {"habitual": med[0] if med and med[1] >= 30 else None, "min30": fh.min30(f["puntos"], ahora)}
        self.almacen.poner_meta(clave, json.dumps({"fecha": iso(ahora), "dato": dato}))
        return dato

    def candidatos(self) -> list[Chollo]:
        ahora = self.ahora or ahora_utc()
        self.consultas = 0
        ajustes = self.cfg["descubrimiento"]["fuentes"][self.nombre]
        vistos, out, fallos = set(), [], 0
        for campana in ajustes.get("campanas", CAMPANAS_MM):
            try:
                html = self.descargar("%s/es/campaign/%s" % (MEDIAMARKT, campana))
            except terceros.ErrorTercero as exc:
                fallos += 1
                print("  mediamarkt: %s no disponible: %s" % (campana, exc))
                continue
            for p in productos_mediamarkt(html):
                if p["id"] in vistos or not p["precio"] or not p["disponible"]:
                    continue
                vistos.add(p["id"])
                c = Chollo(id=p["id"], titulo=p["titulo"], tienda="MediaMarkt", precio_cent=p["precio"], url=p["url"],
                           publicada=ahora, texto=p["titulo"], imagen=p["imagen"], categoria=p["categoria"],
                           fuente=self.nombre, ean=p["ean"], envio_cent=p["envio"],
                           pagina=terceros.url_reacofertas(p["url"]))
                if p["tachado"] and p["tachado"] > p["precio"]:
                    if p["tachado_tipo"] == "LOP":
                        c.min30_cent = p["tachado"]    # "Precio mas bajo (30 dias)": real
                    else:
                        c.precio_antes_cent = p["tachado"]   # referencia comercial
                        pct = (p["tachado"] - p["precio"]) * 100 / p["tachado"]
                        if pct >= self.cfg["descubrimiento"].get("prefiltro_anunciado_pct", 40):
                            h = self._historia(p["url"], ahora)
                            if h:
                                c.ref_hist_cent, c.min30_cent = h["habitual"], h["min30"] or c.min30_cent
                                c.ref_texto = "mediana de 90 días según reacofertas"
                out.append(c)
        if fallos and not out:
            raise FuenteNoDisponible("ninguna campana de MediaMarkt disponible", timedelta(minutes=30))
        print("  mediamarkt: %d productos en campana, %d historias consultadas" % (len(out), self.consultas))
        return out
