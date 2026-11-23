"""Chollometro detecta, el historico real juzga.

Chollometro es la fuente mas rapida (minutos) de bajadas en PcComponentes,
MediaMarkt o Powerplanet, pero su "antes" es lo que escribe quien publica y
casi todo lo que sale es comida, ropa o viajes. Por eso una publicacion solo
puede avisar si se encuentra el MISMO producto en una web con su historia de
precios y, frente a ella, el descuento es de verdad:

- PcComponentes: indice local de las fichas de pccomtracker (sale de su
  sitemap, permitido en su robots.txt; su buscador esta en /api/, que no lo
  esta). Se renueva una vez por semana, comprimido (unos 3 MB). De la ficha
  se usa el "precio habitual" y el minimo del ultimo ano.
- MediaMarkt y Powerplanet (y PcComponentes si no esta en el indice): el
  buscador de reacofertas (?q=), con la mediana diaria de 90 dias si tiene al
  menos 30 dias de historia.

El emparejamiento es estricto: los numeros del modelo y las variantes (Pro,
Max, Lite, Plus...) tienen que coincidir en los dos sentidos, y se queda el
candidato que mas palabras comparte; si dos empatan, no se arriesga.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import datetime, timedelta

from . import fuentes_historico as fh
from . import terceros
from .almacen import Almacen
from .modelo import ahora_utc, desde_iso, iso

VARIANTES = {"pro", "max", "lite", "plus", "ultra", "mini", "se", "air", "slim", "neo", "fe"}
VACIAS = {"de", "del", "la", "el", "los", "las", "con", "para", "por", "sin", "en", "the", "and", "al", "un",
          "una", "oferta", "chollo", "precio", "solo", "hoy", "nuevo", "nueva", "desde", "envio", "gratis",
          "minimo", "historico", "rebaja", "black", "friday", "cyber", "monday", "mas", "muy"}
PRECIO = re.compile(r"\d{1,5}(?:[.,]\d{3})*(?:[.,]\d{1,2})?\s?(?:€|eur\b|euros?\b)", re.I)
INDICE_CADA = timedelta(days=7)
CACHE = timedelta(hours=6)
TIENDAS_REACO = ("PcComponentes", "MediaMarkt", "Powerplanet")


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def tokens(titulo: str, n: int = 8) -> list[str]:
    """Palabras que identifican el producto (las primeras n), sin precios."""
    t = PRECIO.sub(" ", _norm(titulo))
    t = re.split(r"\s[-|–]\s|\bpor\s+\d", t)[0]
    out = []
    for w in re.findall(r"[a-z0-9]+", t):
        if w in VACIAS or (len(w) < 3 and w not in VARIANTES and not any(c.isdigit() for c in w)):
            continue
        if w not in out:
            out.append(w)
    return out[:n]


def puntuacion(post: list[str], candidato: str) -> float:
    """Parecido entre la publicacion y un candidato (0 si es otro modelo)."""
    cand = set(re.findall(r"[a-z0-9]+", _norm(candidato)))
    if not post:
        return 0.0
    obligatorias = [w for w in post if any(c.isdigit() for c in w) or w in VARIANTES]
    if any(w not in cand for w in obligatorias):
        return 0.0
    # Una variante que el candidato tiene y la publicacion no: es otro modelo.
    cabeza = set(tokens(candidato, 10))
    if any(v in cabeza and v not in post for v in VARIANTES):
        return 0.0
    return sum(w in cand for w in post) / len(post)


def elegir(post_titulo: str, candidatos: list[tuple[str, object]], minimo: float = 0.75):
    post = tokens(post_titulo)
    notas = sorted(((puntuacion(post, t), i) for i, (t, _) in enumerate(candidatos)), reverse=True)
    if not notas or notas[0][0] < minimo:
        return None
    if len(notas) > 1 and notas[1][0] == notas[0][0]:
        return None  # dos igual de parecidos: no se arriesga
    return candidatos[notas[0][1]][1]


class Juez:
    def __init__(self, cfg: dict, almacen: Almacen, descargar=None, dormir=time.sleep):
        self.cfg, self.almacen = cfg, almacen
        self.descargar = descargar or terceros.descargar
        self.dormir = dormir
        self.consultas = 0
        almacen.db.execute("CREATE TABLE IF NOT EXISTS pccomtracker_indice (slug TEXT PRIMARY KEY)")

    # ------------------------------------------------------------ indice
    def actualizar_indice(self, ahora: datetime, forzar: bool = False) -> int:
        hecho = self.almacen.meta("pccomtracker_indice_fecha")
        if not forzar and hecho and ahora - desde_iso(hecho) < INDICE_CADA:
            return 0
        slugs = []
        for i in range(400):
            try:
                xml = self.descargar("%s/sitemap/%d.xml" % (fh.PCT, i))
            except terceros.ErrorTercero:
                break
            nuevos = re.findall(r"<loc>https://pccomtracker\.com/producto/([^<]+)</loc>", xml)
            if not nuevos and i > 0:
                break
            slugs.extend(nuevos)
            self.dormir(0.5)
        if slugs:
            self.almacen.db.execute("DELETE FROM pccomtracker_indice")
            self.almacen.db.executemany("INSERT OR IGNORE INTO pccomtracker_indice VALUES (?)", [(s,) for s in slugs])
            self.almacen.db.commit()
            self.almacen.poner_meta("pccomtracker_indice_fecha", iso(ahora))
            print("Juez: indice de pccomtracker con %d fichas" % len(slugs))
        return len(slugs)

    def buscar_pccomtracker(self, titulo: str) -> str | None:
        post = tokens(titulo)
        claves = sorted((w for w in post if w not in VARIANTES), key=len, reverse=True)[:2]
        if len(claves) < 2:
            return None
        filas = self.almacen.db.execute("SELECT slug FROM pccomtracker_indice WHERE slug LIKE ? AND slug LIKE ?",
                                        ("%%%s%%" % claves[0], "%%%s%%" % claves[1])).fetchall()
        slug = elegir(titulo, [(f["slug"].replace("-", " "), f["slug"]) for f in filas])
        return "%s/producto/%s" % (fh.PCT, slug) if slug else None

    # ---------------------------------------------------------- consulta
    def _cacheado(self, clave: str, ahora: datetime, calcular):
        g = self.almacen.meta(clave)
        if g:
            g = json.loads(g)
            if ahora - desde_iso(g["fecha"]) < CACHE:
                return g["dato"]
        if self.consultas >= self.cfg["descubrimiento"]["comunidad"].get("max_consultas", 10):
            return None
        self.consultas += 1
        dato = calcular()
        self.almacen.poner_meta(clave, json.dumps({"fecha": iso(ahora), "dato": dato}))
        return dato

    def _por_pccomtracker(self, c) -> dict | None:
        url = self.buscar_pccomtracker(c.titulo)
        if not url:
            return None
        f = fh.ficha_pccomtracker(self.descargar(url))
        if not f or not f["habitual"]:
            return None
        return {"ref": f["habitual"], "texto": "precio habitual según pccomtracker", "minimo": f["minimo"],
                "minimo_fecha": iso(f["minimo_fecha"]) if f["minimo_fecha"] else None, "minimo_texto": "del año",
                "pagina": url, "titulo": f["titulo"]}

    def _por_reacofertas(self, c, ahora: datetime) -> dict | None:
        from urllib.parse import quote
        consulta = " ".join(tokens(c.titulo, 5))
        tarjetas = fh.tarjetas_reacofertas(self.descargar(fh.REACO + "?q=" + quote(consulta)))
        id_ = elegir(c.titulo, [(x["titulo"], x["id"]) for x in tarjetas])
        if not id_:
            return None
        pagina = "%s?id=%s" % (fh.REACO, id_)
        f = fh.ficha_reacofertas(self.descargar(pagina))
        if not f or (f["tienda"] and f["tienda"] != c.tienda):
            return None
        med = fh.mediana_diaria(f["puntos"], ahora)
        if not med or med[1] < 30:
            return None
        ventana = [(d, p) for d, p in f["puntos"] if d.date() < ahora.date() and d >= ahora - timedelta(days=180)]
        mn = min(ventana, key=lambda v: (v[1], v[0])) if ventana else None
        return {"ref": med[0], "texto": "mediana de 90 días según reacofertas", "minimo": mn[1] if mn else None,
                "minimo_fecha": iso(mn[0]) if mn else None, "minimo_texto": "de 6 meses", "pagina": pagina,
                "titulo": f["titulo"]}

    def juzgar(self, c, ahora: datetime | None = None) -> bool:
        """Anade a la publicacion su referencia real, si se encuentra el mismo
        producto. True si la tiene."""
        ahora = ahora or ahora_utc()
        tienda = fh._tienda(c.tienda or "")
        if tienda not in TIENDAS_REACO:
            return False
        c.tienda = tienda

        def calcular():
            try:
                return (self._por_pccomtracker(c) if tienda == "PcComponentes" else None) \
                    or self._por_reacofertas(c, ahora)
            except terceros.ErrorTercero as exc:
                print("  juez: %s" % exc)
                return None
        dato = self._cacheado("juez:%s:%s" % (tienda, " ".join(tokens(c.titulo))), ahora, calcular)
        if not dato:
            return False
        c.ref_hist_cent, c.ref_texto = dato["ref"], dato["texto"]
        c.minimo_cent, c.minimo_texto = dato["minimo"], dato["minimo_texto"]
        c.minimo_fecha = desde_iso(dato["minimo_fecha"]) if dato["minimo_fecha"] else None
        c.pagina = dato["pagina"]
        print("  juez: %s -> %s (%s)" % (c.titulo[:60], dato["titulo"][:60] if dato["titulo"] else "?",
                                         dato["texto"]))
        return True
