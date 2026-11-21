"""Almacenamiento en SQLite (data/precios.db).

- observaciones: cada lectura valida (precio en centimos, condicion, fuente...)
- fallos: las lecturas fallidas, aparte, para no pisar nunca el ultimo precio bueno
- estado: lo que cada producto necesita recordar entre ciclos (JSON por clave)
- avisos: los avisos enviados, para no repetirlos (tambien tras reiniciar Docker)
- historial_externo: historicos importados (p. ej. el grafico de 365 dias)
- candidatos: lo que el modo descubrimiento vio y no notifico

El CSV antiguo (historico_precios.csv) se migra una vez, con copia previa, y se
puede migrar otra vez sin duplicar nada.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .modelo import NUEVO, OFERTA_DESTACADA, Observacion, a_centimos, ahora_utc, desde_iso, iso

ESQUEMA = """
CREATE TABLE IF NOT EXISTS observaciones (
    id INTEGER PRIMARY KEY,
    clave TEXT NOT NULL,
    fecha TEXT NOT NULL,
    fuente TEXT NOT NULL,
    tipo_precio TEXT NOT NULL,
    condicion TEXT NOT NULL,
    disponible INTEGER NOT NULL,
    precio_cent INTEGER,
    cupon_cent INTEGER,
    cupon_texto TEXT,
    otra_condicion TEXT,
    precio_otra_cent INTEGER,
    vendedor TEXT,
    origen TEXT NOT NULL DEFAULT 'bot',
    UNIQUE (clave, fecha, fuente, origen)
);
CREATE INDEX IF NOT EXISTS obs_serie ON observaciones (clave, condicion, tipo_precio, fecha);
CREATE TABLE IF NOT EXISTS fallos (
    id INTEGER PRIMARY KEY, clave TEXT NOT NULL, fecha TEXT NOT NULL,
    fuente TEXT NOT NULL, error TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fallos_clave ON fallos (clave, fecha);
CREATE TABLE IF NOT EXISTS estado (clave TEXT PRIMARY KEY, datos TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS avisos (
    id INTEGER PRIMARY KEY, clave TEXT NOT NULL, evento TEXT NOT NULL,
    condicion TEXT NOT NULL, tipo_precio TEXT NOT NULL, precio_cent INTEGER,
    fecha TEXT NOT NULL, modo TEXT NOT NULL, canal TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS avisos_clave ON avisos (clave, evento, condicion, tipo_precio);
CREATE TABLE IF NOT EXISTS historial_externo (
    clave TEXT NOT NULL, fuente TEXT NOT NULL, captura TEXT NOT NULL,
    fecha TEXT NOT NULL, precio_cent INTEGER NOT NULL,
    PRIMARY KEY (clave, fuente, captura, fecha)
);
CREATE TABLE IF NOT EXISTS candidatos (
    id INTEGER PRIMARY KEY, asin TEXT NOT NULL, fecha TEXT NOT NULL,
    datos TEXT NOT NULL, notificado INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
"""

# Ids del bot antiguo -> clave nueva. Lo que no este aqui se conserva como "legacy:<id>".
IDS_ANTIGUOS = {
    "aspiradora_xiaomi_g20_amazon": ("B0DJ82JKFL", "amazon_ficha"),
    "silla_b100_amazon": ("B0G5WQ65P2", "amazon_ficha"),
    "silla_flexispot_ergox_lite_amazon": ("B0H3ZVDKYV", "amazon_ficha"),
    "silla_b100_oficial": ("shopify:48150078357718", "shopify"),
}


class Almacen:
    def __init__(self, ruta: str | Path):
        self.ruta = str(ruta)
        if self.ruta != ":memory:":
            Path(self.ruta).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.ruta)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(ESQUEMA)
        self.db.commit()

    def copia_en_memoria(self) -> "Almacen":
        """Para el modo prueba: se trabaja sobre una copia y no se guarda nada."""
        copia = Almacen(":memory:")
        self.db.backup(copia.db)
        return copia

    def cerrar(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- lecturas
    def registrar(self, obs: Observacion, origen: str = "bot") -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO observaciones (clave, fecha, fuente, tipo_precio, condicion,"
            " disponible, precio_cent, cupon_cent, cupon_texto, otra_condicion, precio_otra_cent,"
            " vendedor, origen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (obs.clave, iso(obs.fecha), obs.fuente, obs.tipo_precio, obs.condicion,
             int(obs.disponible), obs.precio_cent, obs.cupon_cent, obs.cupon_texto,
             obs.otra_condicion, obs.precio_otra_condicion_cent, obs.vendedor, origen))
        self.db.commit()

    def registrar_fallo(self, clave: str, fuente: str, error: str, fecha: datetime | None = None) -> None:
        self.db.execute("INSERT INTO fallos (clave, fecha, fuente, error) VALUES (?,?,?,?)",
                        (clave, iso(fecha or ahora_utc()), fuente, error[:1000]))
        self.db.commit()

    def serie(self, clave: str, condicion: str = NUEVO, tipo_precio: str = OFERTA_DESTACADA,
              desde: datetime | None = None, hasta: datetime | None = None) -> list[tuple[datetime, int]]:
        """Precios validos de una serie comparable (mismo producto, condicion y tipo),
        ordenados por fecha. Sin stock o sin precio no cuentan."""
        sql = ("SELECT fecha, precio_cent FROM observaciones WHERE clave=? AND condicion=?"
               " AND tipo_precio=? AND disponible=1 AND precio_cent IS NOT NULL")
        args: list = [clave, condicion, tipo_precio]
        if desde:
            sql += " AND fecha>=?"
            args.append(iso(desde))
        if hasta:
            sql += " AND fecha<?"
            args.append(iso(hasta))
        filas = self.db.execute(sql + " ORDER BY fecha", args).fetchall()
        return [(desde_iso(f["fecha"]), f["precio_cent"]) for f in filas]

    def ultima_observacion(self, clave: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM observaciones WHERE clave=? ORDER BY fecha DESC LIMIT 1",
                               (clave,)).fetchone()

    def fallos_recientes(self, clave: str, limite: int = 5) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM fallos WHERE clave=? ORDER BY fecha DESC LIMIT ?",
                               (clave, limite)).fetchall()

    # ------------------------------------------------------------------ estado
    def estado(self, clave: str) -> dict:
        fila = self.db.execute("SELECT datos FROM estado WHERE clave=?", (clave,)).fetchone()
        return json.loads(fila["datos"]) if fila else {}

    def guardar_estado(self, clave: str, datos: dict) -> None:
        self.db.execute("INSERT INTO estado (clave, datos) VALUES (?,?) ON CONFLICT(clave)"
                        " DO UPDATE SET datos=excluded.datos", (clave, json.dumps(datos)))
        self.db.commit()

    # ------------------------------------------------------------------ avisos
    def aviso_enviado(self, clave: str, evento: str, precio_cent: int | None,
                      condicion: str = NUEVO, tipo_precio: str = OFERTA_DESTACADA,
                      desde: datetime | None = None) -> bool:
        sql = ("SELECT 1 FROM avisos WHERE clave=? AND evento=? AND condicion=? AND tipo_precio=?"
               " AND precio_cent IS ?")
        args: list = [clave, evento, condicion, tipo_precio, precio_cent]
        if desde:
            sql += " AND fecha>=?"
            args.append(iso(desde))
        return self.db.execute(sql, args).fetchone() is not None

    def registrar_aviso(self, clave: str, evento: str, precio_cent: int | None, modo: str,
                        canal: str, condicion: str = NUEVO, tipo_precio: str = OFERTA_DESTACADA,
                        fecha: datetime | None = None) -> None:
        self.db.execute("INSERT INTO avisos (clave, evento, condicion, tipo_precio, precio_cent,"
                        " fecha, modo, canal) VALUES (?,?,?,?,?,?,?,?)",
                        (clave, evento, condicion, tipo_precio, precio_cent,
                         iso(fecha or ahora_utc()), modo, canal))
        self.db.commit()

    def avisos_desde(self, modo: str, desde: datetime) -> int:
        return self.db.execute("SELECT COUNT(*) FROM avisos WHERE modo=? AND fecha>=?",
                               (modo, iso(desde))).fetchone()[0]

    # ---------------------------------------------------- historiales externos
    def importar_historial(self, clave: str, fuente: str, captura: datetime,
                           puntos: list[tuple[datetime, int]]) -> int:
        cur = self.db.executemany(
            "INSERT OR IGNORE INTO historial_externo (clave, fuente, captura, fecha, precio_cent)"
            " VALUES (?,?,?,?,?)", [(clave, fuente, iso(captura), iso(f), p) for f, p in puntos])
        self.db.commit()
        return cur.rowcount

    def historial_externo(self, clave: str, fuente: str) -> tuple[datetime, list[tuple[datetime, int]]] | None:
        """La captura mas reciente de esa fuente, con sus puntos."""
        fila = self.db.execute("SELECT MAX(captura) AS c FROM historial_externo WHERE clave=? AND fuente=?",
                               (clave, fuente)).fetchone()
        if not fila or not fila["c"]:
            return None
        puntos = self.db.execute("SELECT fecha, precio_cent FROM historial_externo WHERE clave=?"
                                 " AND fuente=? AND captura=? ORDER BY fecha", (clave, fuente, fila["c"]))
        return desde_iso(fila["c"]), [(desde_iso(p["fecha"]), p["precio_cent"]) for p in puntos]

    # -------------------------------------------------------------- candidatos
    def registrar_candidato(self, asin: str, datos: dict, notificado: bool) -> None:
        self.db.execute("INSERT INTO candidatos (asin, fecha, datos, notificado) VALUES (?,?,?,?)",
                        (asin, iso(ahora_utc()), json.dumps(datos, ensure_ascii=False), int(notificado)))
        self.db.commit()

    # -------------------------------------------------------------------- meta
    def meta(self, clave: str) -> str | None:
        fila = self.db.execute("SELECT valor FROM meta WHERE clave=?", (clave,)).fetchone()
        return fila["valor"] if fila else None

    def poner_meta(self, clave: str, valor: str) -> None:
        self.db.execute("INSERT INTO meta (clave, valor) VALUES (?,?) ON CONFLICT(clave)"
                        " DO UPDATE SET valor=excluded.valor", (clave, valor))
        self.db.commit()

    # --------------------------------------------------------------- migracion
    def migrar_csv(self, ruta_csv: Path, carpeta_copias: Path) -> dict:
        """Pasa el historico CSV antiguo a SQLite. Idempotente: las filas ya
        migradas se ignoran. Antes de la primera migracion copia el CSV."""
        if not ruta_csv.exists():
            return {"filas_csv": 0, "nuevas": 0, "ya_estaban": 0}
        carpeta_copias.mkdir(parents=True, exist_ok=True)
        copia = carpeta_copias / ("%s.antes-de-migrar" % ruta_csv.name)
        if not copia.exists():
            shutil.copy2(ruta_csv, copia)

        # Las filas que ya escribe el bot nuevo (id = ASIN o "shopify:...") estan
        # tambien en SQLite: no se vuelven a migrar.
        filas = [f for f in csv.DictReader(ruta_csv.open(encoding="utf-8", newline=""))
                 if not re.match(r"^([A-Z0-9]{10}|shopify:.+)$", f["id"])]
        nuevas = 0
        for f in filas:
            clave, fuente = IDS_ANTIGUOS.get(f["id"], ("legacy:%s" % f["id"], "desconocida"))
            final = a_centimos(float(f["precio_eur"]))
            sin_cupon = f.get("precio_sin_cupon_eur") or ""
            base = a_centimos(float(sin_cupon)) if sin_cupon else final
            cur = self.db.execute(
                "INSERT OR IGNORE INTO observaciones (clave, fecha, fuente, tipo_precio, condicion,"
                " disponible, precio_cent, cupon_cent, cupon_texto, origen) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (clave, iso(desde_iso(f["fecha_utc"])), fuente, OFERTA_DESTACADA, NUEVO,
                 int(f["disponible"].strip().lower() == "true"), base,
                 (base - final) or None, f.get("cupon") or None, "csv_migrado"))
            nuevas += cur.rowcount
        self.db.commit()
        total = self.db.execute("SELECT COUNT(*) FROM observaciones WHERE origen='csv_migrado'").fetchone()[0]
        return {"filas_csv": len(filas), "nuevas": nuevas, "ya_estaban": len(filas) - nuevas,
                "en_sqlite": total}

    def migrar_state_json(self, ruta: Path) -> int:
        """Trae del state.json antiguo lo que sigue teniendo sentido (una sola vez)."""
        if self.meta("state_json_migrado") or not ruta.exists():
            return 0
        antiguo = json.loads(ruta.read_text(encoding="utf-8"))
        n = 0
        for id_antiguo, (clave, _fuente) in IDS_ANTIGUOS.items():
            datos = antiguo.get(id_antiguo)
            if not isinstance(datos, dict):
                continue
            est = self.estado(clave)
            if datos.get("below_threshold_notified"):
                est["objetivo_alcanzado"] = True
            if datos.get("last_available") is False:
                est["disponible"] = False
            self.guardar_estado(clave, est)
            n += 1
        self.poner_meta("state_json_migrado", iso(ahora_utc()))
        return n

    def exportar_csv(self, ruta: Path) -> int:
        filas = self.db.execute("SELECT clave, fecha, fuente, condicion, tipo_precio, disponible,"
                                " precio_cent, cupon_cent, cupon_texto, vendedor, origen"
                                " FROM observaciones ORDER BY fecha").fetchall()
        with ruta.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["fecha_utc", "clave", "fuente", "condicion", "tipo_precio", "disponible",
                        "precio_eur", "cupon_eur", "cupon", "vendedor", "origen"])
            for r in filas:
                w.writerow([r["fecha"], r["clave"], r["fuente"], r["condicion"], r["tipo_precio"],
                            bool(r["disponible"]),
                            "" if r["precio_cent"] is None else "%.2f" % (r["precio_cent"] / 100),
                            "" if r["cupon_cent"] is None else "%.2f" % (r["cupon_cent"] / 100),
                            r["cupon_texto"] or "", r["vendedor"] or "", r["origen"]])
        return len(filas)
