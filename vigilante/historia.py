"""Historico de todo lo que el descubrimiento ve, y deduplicacion entre fuentes.

Cada oferta que pasa por el bot (Amazon, PcComponentes via pccomtracker o
Chollometro, MediaMarkt, Powerplanet...) se guarda en precios_vistos con lo
que se sepa: precio, minimo de 30 dias, precio habitual, PVPR o "antes",
envio, disponibilidad e identificadores (ASIN, EAN, SKU, referencia del
fabricante). Con eso:

- se calcula una referencia propia (la mediana de lo visto en 30 dias), que
  permite detectar CAIDAS ANOMALAS aunque la tienda no anuncie descuento;
- la misma oferta que llega por varias fuentes (pccomtracker y Chollometro,
  por ejemplo) tiene una clave comun y avisa una sola vez;
- se vuelve a avisar solo si el precio baja otra vez de verdad (por defecto
  un 5 % por debajo del ultimo aviso).
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from datetime import datetime, timedelta

from .almacen import Almacen
from .modelo import desde_iso, iso

TABLA = """
CREATE TABLE IF NOT EXISTS precios_vistos (
    clave TEXT NOT NULL, fecha TEXT NOT NULL, fuente TEXT NOT NULL, tienda TEXT,
    precio INTEGER, min30 INTEGER, habitual INTEGER, anunciado INTEGER, envio INTEGER,
    disponible INTEGER, asin TEXT, ean TEXT, sku TEXT, mpn TEXT, titulo TEXT,
    PRIMARY KEY (clave, fecha, fuente));
CREATE INDEX IF NOT EXISTS precios_vistos_clave ON precios_vistos (clave, fecha);
"""
EVENTO = "oportunidad"
VACIAS = {"de", "del", "la", "el", "los", "las", "con", "para", "por", "sin", "en", "y", "the", "and", "a"}


def preparar(almacen: Almacen) -> None:
    almacen.db.executescript(TABLA)


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def clave(tienda: str, asin: str | None = None, ean: str | None = None, titulo: str | None = None) -> str:
    """Clave comun de una oferta: la misma tienda y el mismo producto, venga de
    donde venga. ASIN en Amazon; EAN si se sabe; si no, la firma del titulo
    (las primeras palabras significativas, que pccomtracker, reacofertas y la
    tienda copian igual)."""
    t = re.sub(r"\W+", "", _norm(tienda)) or "tienda"
    if asin:
        return "amazon:%s" % asin
    if ean:
        return "%s:ean:%s" % (t, str(ean).lstrip("0"))
    palabras = [w for w in re.findall(r"[a-z0-9]+", _norm(titulo)) if w not in VACIAS][:8]
    return "%s:%s" % (t, "-".join(palabras))


def registrar(almacen: Almacen, k: str, fuente: str, tienda: str, precio: int | None, ahora: datetime,
              **datos) -> None:
    """Una fila por oferta y fuente si cambia el precio o cada 6 h."""
    preparar(almacen)
    ultima = almacen.db.execute("SELECT fecha, precio FROM precios_vistos WHERE clave=? AND fuente=?"
                                " ORDER BY fecha DESC LIMIT 1", (k, fuente)).fetchone()
    if ultima and ultima["precio"] == precio and ahora - desde_iso(ultima["fecha"]) < timedelta(hours=6):
        return
    campos = ("min30", "habitual", "anunciado", "envio", "disponible", "asin", "ean", "sku", "mpn", "titulo")
    almacen.db.execute(
        "INSERT OR REPLACE INTO precios_vistos (clave, fecha, fuente, tienda, precio, %s) VALUES (?,?,?,?,?,%s)"
        % (", ".join(campos), ",".join("?" * len(campos))),
        [k, iso(ahora), fuente, tienda, precio] + [datos.get(c) for c in campos])
    almacen.db.commit()


def referencia_bot(almacen: Almacen, k: str, ahora: datetime, dias: int = 30, min_lecturas: int = 3,
                   min_dias: int = 3) -> int | None:
    """Mediana de lo visto en los ultimos 'dias' (sin contar las ultimas 2 h),
    si hay lecturas suficientes en dias distintos."""
    preparar(almacen)
    filas = almacen.db.execute(
        "SELECT fecha, precio FROM precios_vistos WHERE clave=? AND precio IS NOT NULL AND disponible IS NOT 0"
        " AND fecha>=? AND fecha<?", (k, iso(ahora - timedelta(days=dias)), iso(ahora - timedelta(hours=2)))).fetchall()
    if len(filas) < min_lecturas or len({f["fecha"][:10] for f in filas}) < min_dias:
        return None
    return int(statistics.median(f["precio"] for f in filas))


def ya_avisada(almacen: Almacen, k: str, precio: int, bajada_pct: float) -> bool:
    """True si ya se aviso de esta oferta a este precio o a uno parecido. Se
    vuelve a avisar si ahora es al menos bajada_pct % mas barata."""
    fila = almacen.db.execute("SELECT precio_cent FROM avisos WHERE clave=? AND evento=? ORDER BY fecha DESC LIMIT 1",
                              (k, EVENTO)).fetchone()
    if not fila or fila["precio_cent"] is None:
        return False
    return precio > fila["precio_cent"] * (1 - bajada_pct / 100)


def anotar_aviso(almacen: Almacen, k: str, precio: int, modo: str, canal: str, ahora: datetime) -> None:
    almacen.registrar_aviso(k, EVENTO, precio, modo, canal, fecha=ahora)
