"""Importacion MANUAL del historial de precios de 365 dias de Amazon.

Amazon muestra ese grafico en la ficha ("Historial de precios"), pero llega por
un canal interno de su asistente de compras que exige tu sesion iniciada y un
token anti-CSRF. No es una API publica, asi que el bot NO lo consulta solo.

Lo que si se puede hacer: abres tu el grafico en tu navegador, guardas la
captura de red (HAR) y el bot la lee sin conectarse a nada. Del HAR solo se
toma el JSON del grafico (PriceHistoryChart); cookies, cabeceras y el resto se
ignoran y no se guardan. Despues, borra el HAR: contiene tu sesion.

Que representa cada punto (segun el propio grafico): "el precio actual mas
bajo de cada dia", sin gastos de envio. Hay ~95 puntos en un ano: NO son
observaciones diarias completas, asi que no se calcula ninguna media con
ellos; solo se usan como cota (minimo y maximo del periodo).
"""
from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .modelo import a_centimos, desde_iso

FUENTE = "amazon_historial_365_manual"


@dataclass
class Grafico:
    captura: datetime                 # cuando se abrio el grafico (UTC)
    puntos: list[tuple[datetime, int]]  # (dia, precio en centimos)
    dias: int                         # periodo que cubre (365)
    aviso: str                        # "Los precios mostrados excluyen los gastos de envio."
    nota_puntos: str                  # que representa cada punto


def _json_del_grafico(texto: str) -> list[dict]:
    """Todos los PriceHistoryChart que aparezcan en un texto (doblemente escapados
    dentro de un atributo HTML dentro de un flujo de eventos)."""
    graficos = []
    for m in re.finditer(r"priceChartJson", texto):
        trozo = texto[m.start():m.start() + 400000]
        trozo = html.unescape(trozo.replace('\\"', '"').replace("\\\\", "\\"))
        try:
            ini = trozo.index('"{') + 1
        except ValueError:
            continue
        prof = 0
        for j in range(ini, len(trozo)):
            if trozo[j] == "{":
                prof += 1
            elif trozo[j] == "}":
                prof -= 1
                if prof == 0:
                    try:
                        datos = json.loads(trozo[ini:j + 1].replace('\\"', '"'))
                    except json.JSONDecodeError:
                        break
                    if datos.get("type") == "PriceHistoryChart":
                        graficos.append(datos)
                    break
    return graficos


def _puntos(datos: dict, captura: datetime) -> tuple[list[tuple[datetime, int]], str]:
    """Usa la ventana de 1 ano si existe. El eje X va de 1 a 365, y 365 es el dia
    de la captura (comprobado: la ventana 1M de la muestra va de 336 = 23 oct a
    365 = 21 nov, con la captura hecha el 21 nov)."""
    lista, nota = datos.get("dataPoints") or [], ""
    for w in datos.get("windows") or []:
        if w.get("label") in ("1A", "1Y", "12M") and w.get("overrideDataPoints"):
            lista = w["overrideDataPoints"]
            nota = (w.get("disclaimers") or {}).get("belowChart", "")
    hoy = captura.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    puntos = []
    for p in lista:
        x, y = p.get("x", {}).get("value"), p.get("y", {}).get("value")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or y <= 0:
            continue
        puntos.append((hoy - timedelta(days=365 - int(x)), a_centimos(y)))
    return sorted(puntos), nota


def desde_har(ruta: str, asin: str) -> Grafico:
    """Lee un HAR guardado a mano. Comprueba que la peticion del grafico sea de
    ese ASIN para no importar el historial de otro producto."""
    har = json.load(open(ruta, encoding="utf-8"))
    for e in har.get("log", {}).get("entries", []):
        c = e.get("response", {}).get("content", {})
        texto = c.get("text") or ""
        if c.get("encoding") == "base64":
            texto = base64.b64decode(texto).decode("utf-8", "replace")
        if "PriceHistoryChart" not in texto:
            continue
        contexto = (e.get("request", {}).get("postData", {}) or {}).get("text", "") + \
            e.get("request", {}).get("url", "")
        if asin not in contexto and asin not in texto:
            raise ValueError("El grafico del HAR no es del ASIN %s" % asin)
        graficos = _json_del_grafico(texto)
        if not graficos:
            continue
        captura = desde_iso(e.get("startedDateTime", "").replace("Z", "+00:00"))
        puntos, nota = _puntos(graficos[0], captura)
        if not puntos:
            raise ValueError("El grafico no tiene puntos de precio")
        return Grafico(captura, puntos, 365, graficos[0].get("disclaimerLegend", ""), nota)
    raise ValueError("El HAR no contiene el historial de precios (PriceHistoryChart). Abre la "
                     "ficha, pulsa \"Historial de precios\" y guarda el HAR despues.")
