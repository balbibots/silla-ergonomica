"""Fuentes directas: las propias tiendas, sin esperar a que nadie publique la oferta.

Comprobado el 24/11/2026 con una peticion normal identificada como el bot:

| Tienda         | Resultado | Uso |
|----------------|-----------|-----|
| Amazon /deals  | 200, sin CAPTCHA | Lista de ofertas activas (30 por carga) en un JSON de la pagina: ASIN, titulo, descuento ANUNCIADO, tipo y estado. Sin precio: se lee la ficha de cada finalista, que trae el precio, el minimo de 30 dias y el precio mediano o el PVPR |
| PcComponentes  | 403 (Cloudflare) | No. Via pccomtracker, Chollometro + juez, o el feed de Awin |
| MediaMarkt     | 200 | Fichas con JSON-LD y "Precio mas bajo (30 dias)"; los listados y campanas cargan los productos por su API interna (GraphQL): no se usa |
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

import check_price as cp

from . import terceros
from .fuentes import Candidato, FuenteNoDisponible

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
