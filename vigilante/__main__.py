"""Linea de ordenes. Ejemplos (dentro del contenedor, anteponer
"docker compose exec silla-sihoo-bot"):

  python -m vigilante bucle                    # lo que ejecuta Docker
  python -m vigilante seguimiento --prueba     # una pasada sin enviar ni guardar nada
  python -m vigilante descubrimiento --prueba
  python -m vigilante anadir <ASIN o URL> --nombre "..." --categoria "..." --objetivo 120
  python -m vigilante quitar <ASIN o URL>
  python -m vigilante listar
  python -m vigilante estado
  python -m vigilante importar-historial archivo.har --asin B0DJ82JKFL
  python -m vigilante exportar-csv data/exportacion.csv
  python -m vigilante migrar
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import amazon, config, descubrimiento, fuentes, historial_amazon, notificadores, reglas, seguimiento
from .almacen import Almacen
from .modelo import ahora_utc, desde_iso, eur, fecha_corta, iso

TICK_SEG = 5 * 60


def _data() -> Path:
    return Path(os.environ.get("DATA_DIR", config.RAIZ / "data"))


def abrir_almacen() -> Almacen:
    data = _data()
    almacen = Almacen(data / "precios.db")
    migrar(almacen, data, silencioso=True)
    return almacen


def migrar(almacen: Almacen, data: Path, silencioso: bool = False) -> None:
    csv_antiguo = Path(os.environ.get("HISTORY_FILE", data / "historico_precios.csv"))
    if not almacen.meta("csv_migrado"):
        r = almacen.migrar_csv(csv_antiguo, data / "copias")
        almacen.poner_meta("csv_migrado", iso(ahora_utc()))
        print("Migracion del CSV: %(filas_csv)d filas, %(nuevas)d nuevas, %(ya_estaban)d ya estaban" % r)
        if r["filas_csv"] and r.get("en_sqlite", 0) < r["filas_csv"]:
            print("  OJO: hay menos filas migradas que en el CSV (filas repetidas en el CSV)")
    elif not silencioso:
        r = almacen.migrar_csv(csv_antiguo, data / "copias")
        print("CSV ya migrado. Comprobacion: %(filas_csv)d filas, %(nuevas)d nuevas" % r)
    n = almacen.migrar_state_json(Path(os.environ.get("STATE_FILE", data / "state.json")))
    if n:
        print("Migrado el estado antiguo de %d producto(s)" % n)


def _fuentes(almacen: Almacen) -> list:
    lista = [fuentes.CholloMazon(almacen)]
    simulada = os.environ.get("FUENTE_SIMULADA")
    if simulada:
        lista.append(fuentes.Simulada(simulada))
    return lista


def _descubrir(cfg: dict, almacen: Almacen, notificador, ahora=None) -> int:
    seguidos = {p.clave for p in config.productos(cfg)}
    return descubrimiento.ejecutar(cfg, almacen, notificador, _fuentes(almacen), ahora or ahora_utc(),
                                   leer=amazon.leer, seguidos=seguidos)


def cmd_bucle(_args) -> int:
    cfg = config.cargar()
    notificador = notificadores.crear(cfg["canal"])
    almacen = abrir_almacen()
    activas = [f for f, a in cfg["descubrimiento"]["fuentes"].items() if a.get("activo")]
    print("Vigilante arrancado. Canal: %s. Productos: %d. Descubrimiento: %s."
          % (notificador.nombre, len(cfg["productos"]),
             ("activo (%s)" % ", ".join(activas)) if cfg["descubrimiento"]["activo"] else "desactivado"))
    while True:
        try:
            cfg = config.cargar()  # recoge los productos anadidos o quitados sin reiniciar
            # Primero el descubrimiento: puede pedir una lectura extra de un producto seguido.
            if cfg["descubrimiento"]["activo"]:
                try:
                    _descubrir(cfg, almacen, notificador)
                except Exception as exc:  # noqa: BLE001 - un modo no tumba al otro
                    print("Descubrimiento: error: %s: %s" % (type(exc).__name__, exc))
            if cfg["seguimiento"]["activo"]:
                seguimiento.ciclo(cfg, almacen, notificador)
        except config.ErrorConfig as exc:
            print("Configuracion no valida, no hago nada hasta que se corrija: %s" % exc)
        except Exception as exc:  # noqa: BLE001
            print("ERROR INESPERADO en esta vuelta: %s: %s" % (type(exc).__name__, exc))
        time.sleep(TICK_SEG)


def cmd_seguimiento(args) -> int:
    cfg = config.cargar()
    almacen = abrir_almacen()
    if args.prueba:
        almacen = almacen.copia_en_memoria()
        for p in config.productos(cfg):  # en prueba se leen todos, toque o no
            est = almacen.estado(p.clave)
            est.pop("ultimo_intento", None)
            almacen.guardar_estado(p.clave, est)
    n = seguimiento.ciclo(cfg, almacen, notificadores.crear(cfg["canal"], args.prueba))
    print("%d aviso(s)%s" % (n, " (modo prueba: nada enviado ni guardado)" if args.prueba else ""))
    return 0


def cmd_descubrimiento(args) -> int:
    cfg = config.cargar()
    cfg["descubrimiento"]["activo"] = True
    almacen = abrir_almacen()
    if args.prueba:
        almacen = almacen.copia_en_memoria()
        # En prueba se ignoran la espera entre descargas y la linea base.
        for f in cfg["descubrimiento"]["fuentes"]:
            almacen.db.execute("DELETE FROM meta WHERE clave IN (?, ?)", ("proxima_%s" % f, "chollomazon_etag"))
            if not almacen.meta("inicio_%s" % f):
                almacen.poner_meta("inicio_%s" % f, "2000-01-01T00:00:00+00:00")
    _descubrir(cfg, almacen, notificadores.crear(cfg["canal"], args.prueba))
    return 0


def cmd_anadir(args) -> int:
    cfg = config.cargar()
    nuevo, asin = config.anadir_producto(cfg, args.producto, args.nombre, args.categoria, args.objetivo)
    config.guardar(nuevo)
    print("Anadido %s. Se leera en la siguiente vuelta del bucle." % asin)
    return 0


def cmd_quitar(args) -> int:
    config.guardar(config.quitar_producto(config.cargar(), args.producto))
    print("Quitado. Su historico se conserva en la base de datos.")
    return 0


def cmd_listar(_args) -> int:
    for p in config.productos(config.cargar()):
        obj = " | objetivo %s" % eur(p.precio_objetivo_cent) if p.precio_objetivo_cent else ""
        print("%-24s %-30s %s%s" % (p.clave, p.nombre[:30], p.categoria, obj))
    return 0


def cmd_estado(_args) -> int:
    cfg = config.cargar()
    almacen = abrir_almacen()
    print("Canal: %s | descubrimiento: %s" % (cfg["canal"], "activo" if cfg["descubrimiento"]["activo"] else "desactivado"))
    for p in config.productos(cfg):
        est = almacen.estado(p.clave)
        serie = almacen.serie(p.clave)
        ult = serie[-1] if serie else None
        print("\n%s (%s)" % (p.nombre, p.clave))
        print("  ultimo precio valido: %s" % ("%s el %s" % (eur(ult[1]), fecha_corta(ult[0])) if ult else "ninguno"))
        if serie:
            mn = min(serie, key=lambda x: x[1])
            print("  minimo registrado: %s el %s (desde %s, %d lecturas)"
                  % (eur(mn[1]), fecha_corta(mn[0])[:10], fecha_corta(serie[0][0])[:10], len(serie)))
        print("  unidades nuevas: %s" % ("no" if est.get("disponible") is False else "si"))
        if est.get("ultimo_intento"):
            print("  ultimo intento: %s | fallos seguidos: %d" % (fecha_corta(desde_iso(est["ultimo_intento"])), est.get("fallos", 0)))
        for f in almacen.fallos_recientes(p.clave, 1) if est.get("fallos") else []:
            print("  ultimo fallo: %s" % f["error"][:150])
        ext = almacen.historial_externo(p.clave, historial_amazon.FUENTE)
        if ext:
            lecturas = [desde_iso(r["fecha"]) for r in almacen.db.execute(
                "SELECT fecha FROM observaciones WHERE clave=? AND fecha>=?", (p.clave, iso(ext[0])))]
            vigente = reglas.minimo_365(ext, serie, lecturas, ahora_utc(),
                                        cfg["seguimiento"]["reglas"]["historial_caducidad_dias"]) is not None
            print("  historial 365 d importado el %s (%d puntos): %s"
                  % (fecha_corta(ext[0])[:10], len(ext[1]),
                     "vigente" if vigente else "NO vigente (huecos de lectura o caducado): reimportar"))
        if est.get("aviso_pendiente"):
            print("  hay un aviso pendiente de enviar")
    return 0


def cmd_importar(args) -> int:
    from .modelo import asin_desde
    asin = asin_desde(args.asin)
    g = historial_amazon.desde_har(args.har, asin)
    almacen = abrir_almacen()
    n = almacen.importar_historial(asin, historial_amazon.FUENTE, g.captura, g.puntos)
    precios = [p for _, p in g.puntos]
    print("Importados %d puntos de %s (captura %s): minimo %s, maximo %s."
          % (n, asin, fecha_corta(g.captura), eur(min(precios)), eur(max(precios))))
    print("Nota del grafico: %s %s" % (g.nota_puntos, g.aviso))
    print("Ahora borra el HAR: contiene tu sesion de Amazon.")
    return 0


def cmd_exportar(args) -> int:
    n = abrir_almacen().exportar_csv(Path(args.ruta))
    print("Exportadas %d lecturas a %s" % (n, args.ruta))
    return 0


def cmd_migrar(_args) -> int:
    migrar(Almacen(_data() / "precios.db"), _data())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vigilante")
    sub = ap.add_subparsers(dest="orden", required=True)
    sub.add_parser("bucle").set_defaults(f=cmd_bucle)
    for nombre, f in (("seguimiento", cmd_seguimiento), ("descubrimiento", cmd_descubrimiento)):
        s = sub.add_parser(nombre)
        s.add_argument("--prueba", action="store_true", help="no envia avisos ni guarda nada")
        s.set_defaults(f=f)
    s = sub.add_parser("anadir")
    s.add_argument("producto", help="ASIN o URL de amazon.es")
    s.add_argument("--nombre")
    s.add_argument("--categoria")
    s.add_argument("--objetivo", type=float, help="precio objetivo en euros")
    s.set_defaults(f=cmd_anadir)
    s = sub.add_parser("quitar")
    s.add_argument("producto")
    s.set_defaults(f=cmd_quitar)
    sub.add_parser("listar").set_defaults(f=cmd_listar)
    sub.add_parser("estado").set_defaults(f=cmd_estado)
    s = sub.add_parser("importar-historial")
    s.add_argument("har")
    s.add_argument("--asin", required=True)
    s.set_defaults(f=cmd_importar)
    s = sub.add_parser("exportar-csv")
    s.add_argument("ruta")
    s.set_defaults(f=cmd_exportar)
    sub.add_parser("migrar").set_defaults(f=cmd_migrar)
    args = ap.parse_args(argv)
    try:
        return args.f(args)
    except (config.ErrorConfig, ValueError, notificadores.ErrorEnvio) as exc:
        print("Error: %s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
