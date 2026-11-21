"""Modo 2: descubrir ofertas por categorias.

No recorre el catalogo: pide candidatos a un PROVEEDOR por categoria, los
filtra, verifica su historico y solo avisa de los mejores (limites por
ejecucion y por dia).

Estado (noviembre de 2026): no hay ninguna fuente gratuita y autorizada que
de candidatos por categoria de Amazon Espana con su historico (ver README,
"Fuentes y autorizacion"). Por eso el modo viene DESACTIVADO y solo existe el
proveedor "simulado", para pruebas. Cuando haya una fuente permitida (p. ej.
la Creators API si llegas a ser elegible), basta con escribir su proveedor.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from .almacen import Almacen
from .modelo import NUEVO, Observacion, desde_iso, fecha_corta, hora_madrid, url_amazon
from .reglas import media_90_dias
from .tarjeta import Tarjeta

MODO = "descubrimiento"


@dataclass
class Candidato:
    asin: str
    titulo: str
    precio_cent: int
    categoria: str
    condicion: str = NUEVO
    disponible: bool = True
    valoracion: float | None = None
    num_valoraciones: int | None = None
    marca: str | None = None
    imagen: str | None = None


@dataclass
class Historico:
    fuente: str                         # "historial de 365 d de Amazon", ...
    puntos: list[tuple[datetime, int]]
    periodo_dias: int
    diario: bool                        # True si hay un valor por dia (permite medias)
    excluye_envio: bool = True


class Proveedor(Protocol):
    nombre: str

    def candidatos(self, categoria: str, limite: int) -> list[Candidato]: ...

    def historial(self, asin: str) -> Historico | None: ...


class ProveedorSimulado:
    """Lee candidatos e historicos de un JSON. Solo para pruebas."""

    nombre = "simulado"

    def __init__(self, ruta: str | Path):
        datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
        self._cand = {c: [Candidato(**x) for x in lista] for c, lista in datos["candidatos"].items()}
        self._hist = {a: Historico(h["fuente"], [(desde_iso(f), p) for f, p in h["puntos"]],
                                   h["periodo_dias"], h["diario"]) for a, h in datos["historicos"].items()}

    def candidatos(self, categoria, limite):
        return self._cand.get(categoria, [])[:limite]

    def historial(self, asin):
        return self._hist.get(asin)


@dataclass
class Hallazgo:
    cand: Candidato
    tipo: str                 # "minimo_365" | "oferta"
    referencia_cent: int      # media de 90 dias verificada
    minimo_cent: int
    periodo: str
    fuente: str
    igualado: bool
    ahorro_cent: int = field(init=False)

    def __post_init__(self):
        self.ahorro_cent = self.referencia_cent - self.cand.precio_cent

    @property
    def puntuacion(self) -> float:
        # Ahorro verificable ponderado por la calidad (valoracion sobre 5).
        return self.ahorro_cent * ((self.cand.valoracion or 0) / 5)


def _en_horario(des: dict, ahora: datetime) -> bool:
    h = des.get("horario")
    if not h:
        return True
    t = hora_madrid(ahora).strftime("%H:%M")
    return h["desde"] <= t < h["hasta"] if h["desde"] <= h["hasta"] else not (h["hasta"] <= t < h["desde"])


def _pasa_filtros(c: Candidato, des: dict) -> bool:
    titulo = c.titulo.lower()
    if des.get("solo_nuevo", True) and (c.condicion != NUEVO or not c.disponible):
        return False
    if not des["precio_min"] * 100 <= c.precio_cent <= des["precio_max"] * 100:
        return False
    if c.valoracion is None or c.valoracion < des["valoracion_min"]:
        return False
    if c.num_valoraciones is None or c.num_valoraciones < des["valoraciones_min"]:
        return False
    if des["marcas"] and (c.marca or "").lower() not in [m.lower() for m in des["marcas"]]:
        return False
    if des["palabras_clave"] and not any(k.lower() in titulo for k in des["palabras_clave"]):
        return False
    if any(k.lower() in titulo for k in des["excluir_palabras"]):
        return False
    return True


def _evaluar(c: Candidato, des: dict, almacen: Almacen, proveedor: Proveedor,
             ahora: datetime) -> Hallazgo | None:
    if des["modo_minimo"] == "externo":
        h = proveedor.historial(c.asin)
        if not h or not h.diario:
            return None  # sin valor diario no hay media de 90 dias verificable
        puntos, periodo = h.puntos, "%d d" % h.periodo_dias
        fuente, tipo = h.fuente, "minimo_365" if h.periodo_dias >= 365 else "oferta"
    else:
        puntos = almacen.serie(c.asin)
        if not puntos:
            return None
        periodo = "observado por el bot desde %s" % fecha_corta(puntos[0][0])[:10]
        fuente, tipo = "Lecturas propias del bot", "oferta"
    puntos = sorted((f, p) for f, p in puntos if f < ahora)
    if not puntos or (ahora - puntos[0][0]).days < des["dias_historial_min"]:
        return None
    media = media_90_dias(puntos, ahora)
    if media is None:
        return None
    ahorro = media - c.precio_cent
    if ahorro < des["ahorro_min_eur"] * 100 or ahorro * 100 < des["descuento_min_pct"] * media:
        return None
    minimo = min(p for _, p in puntos)
    if c.precio_cent > minimo:
        return None
    igualado = c.precio_cent == minimo
    if igualado:
        tipo = "oferta"  # "Nuevo minimo" solo si lo mejora
    return Hallazgo(c, tipo, media, minimo, periodo, fuente, igualado=igualado)


def ejecutar(cfg: dict, almacen: Almacen, notificador, proveedor: Proveedor,
             ahora: datetime) -> int:
    des = cfg["descubrimiento"]
    if not _en_horario(des, ahora):
        print("Descubrimiento: fuera de horario")
        return 0
    hoy = hora_madrid(ahora).replace(hour=0, minute=0, second=0, microsecond=0)
    cupo = min(des["max_avisos_ejecucion"],
               des["max_avisos_dia"] - almacen.avisos_desde(MODO, hoy.astimezone(ahora.tzinfo)))

    vistos, hallazgos, restantes = set(), [], des["max_candidatos"]
    excluidas = {c.lower() for c in des["excluir_categorias"]}
    for categoria in des["categorias"]:
        if categoria.lower() in excluidas or restantes <= 0:
            continue
        lote = proveedor.candidatos(categoria, restantes)
        restantes -= len(lote)
        for c in lote:
            if c.asin in vistos or not _pasa_filtros(c, des):
                continue
            vistos.add(c.asin)
            if des["modo_minimo"] == "local":
                almacen.registrar(Observacion(clave=c.asin, fecha=ahora, fuente=proveedor.nombre,
                                              precio_cent=c.precio_cent))
            h = _evaluar(c, des, almacen, proveedor, ahora)
            if h:
                hallazgos.append(h)

    hallazgos.sort(key=lambda h: h.puntuacion, reverse=True)
    enviados = 0
    for h in hallazgos:
        evento = "%s_%s" % (h.tipo, "igualado" if h.igualado else "nuevo")
        if almacen.aviso_enviado(h.cand.asin, evento, h.cand.precio_cent):
            continue  # nunca el mismo precio dos veces
        if enviados >= max(cupo, 0):
            almacen.registrar_candidato(h.cand.asin, _resumen(h), notificado=False)
            continue
        notificador.enviar(_tarjeta(h, ahora))
        almacen.registrar_aviso(h.cand.asin, evento, h.cand.precio_cent, MODO, notificador.nombre, fecha=ahora)
        almacen.registrar_candidato(h.cand.asin, _resumen(h), notificado=True)
        enviados += 1
    print("Descubrimiento: %d candidatos validos, %d avisos (cupo %d)" % (len(hallazgos), enviados, cupo))
    return enviados


def _resumen(h: Hallazgo) -> dict:
    return {"titulo": h.cand.titulo, "precio_cent": h.cand.precio_cent, "media_90_cent": h.referencia_cent,
            "minimo_cent": h.minimo_cent, "periodo": h.periodo, "fuente": h.fuente}


def _tarjeta(h: Hallazgo, ahora: datetime) -> Tarjeta:
    t = Tarjeta(tipo=h.tipo, nombre=h.cand.titulo, url=url_amazon(h.cand.asin), comprobado=ahora,
                precio_cent=h.cand.precio_cent, referencia_cent=h.referencia_cent,
                referencia_etiqueta="Media 90 d", minimo_anterior_cent=h.minimo_cent,
                minimo_periodo=h.periodo, categoria=h.cand.categoria, imagen=h.cand.imagen,
                asin=h.cand.asin, disponibilidad="✅ En stock · Nuevo",
                fuente="Fuente: %s" % h.fuente, condiciones=["Envío no incluido"])
    if h.igualado:
        t.notas.append("🔁 Iguala el mínimo del periodo (no lo mejora)")
    return t
