"""Configuracion en un JSON editable sin tocar codigo (data/config.json).

Si no existe, se crea a partir de config.example.json. Si es de una version
anterior, se actualiza sola (con copia previa) conservando tus productos,
canal y ajustes. Se valida entera al arrancar: un error detiene el bot con un
mensaje claro en vez de fallar a mitad de un ciclo. Los secretos (tokens,
webhook) van en variables de entorno, nunca aqui.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path

from .modelo import Producto, a_centimos, asin_desde, url_amazon

RAIZ = Path(__file__).resolve().parent.parent
EJEMPLO = RAIZ / "config.example.json"
VERSION = 2
CANALES = ("telegram", "discord", "consola")
FUENTES = ("amazon_ficha", "shopify")
FUENTES_DESCUBRIMIENTO = ("chollomazon", "simulado")


class ErrorConfig(ValueError):
    pass


def ruta_config() -> Path:
    return Path(os.environ.get("CONFIG_FILE", RAIZ / "data" / "config.json"))


def ejemplo() -> dict:
    return json.loads(EJEMPLO.read_text(encoding="utf-8"))


def _completar(cfg: dict, base: dict) -> None:
    """Anade las claves que falten (recursivo) sin tocar las que ya existen."""
    for k, v in base.items():
        if k not in cfg:
            cfg[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(cfg[k], dict) and k != "fuentes":
            _completar(cfg[k], v)


def actualizar_esquema(cfg: dict) -> tuple[dict, list[str]]:
    """Lleva una configuracion antigua a la version actual. Devuelve (cfg, cambios)."""
    cambios = []
    cfg = copy.deepcopy(cfg)
    if cfg.get("version", 1) < 2:
        des = cfg.get("descubrimiento", {})
        # v1: el descubrimiento no tenia fuente real ("proveedor": "ninguno") y
        # venia desactivado por eso. En v2 usa CholloMazon y viene activo.
        for obsoleta in ("proveedor", "modo_minimo", "dias_historial_min", "max_candidatos",
                         "descuento_min_pct", "ahorro_min_eur"):
            des.pop(obsoleta, None)
        if not des.get("activo"):
            des["activo"] = True
            cambios.append("descubrimiento activado con la fuente CholloMazon")
        reglas = cfg.get("seguimiento", {}).get("reglas", {})
        if reglas.get("historial_caducidad_dias", 0) < 400:
            reglas["historial_caducidad_dias"] = 400
            cambios.append("el historial de 365 d importado ya no caduca al mes")
        cfg["version"] = 2
    antes = json.dumps(cfg, sort_keys=True)
    _completar(cfg, ejemplo())
    if json.dumps(cfg, sort_keys=True) != antes:
        cambios.append("anadidos los ajustes nuevos con sus valores por defecto")
    return cfg, cambios


def cargar(ruta: Path | None = None) -> dict:
    ruta = ruta or ruta_config()
    if not ruta.exists():
        ruta.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EJEMPLO, ruta)
        print("Creada la configuracion inicial en %s (copia de config.example.json)" % ruta)
    try:
        cfg = json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ErrorConfig("%s no es un JSON valido: %s" % (ruta, exc)) from exc
    nuevo, cambios = actualizar_esquema(cfg)
    if cambios:
        validar(nuevo)
        copia = ruta.with_name("%s.v%d.bak" % (ruta.name, cfg.get("version", 1)))
        if not copia.exists():
            shutil.copyfile(ruta, copia)
        guardar(nuevo, ruta)
        print("Configuracion actualizada a la version %d (copia en %s): %s"
              % (VERSION, copia.name, "; ".join(cambios)))
        cfg = nuevo
    validar(cfg)
    return cfg


def guardar(cfg: dict, ruta: Path | None = None) -> None:
    validar(cfg)
    ruta = ruta or ruta_config()
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(ruta)


def _num(seccion: dict, clave: str, donde: str, minimo: float = 0) -> None:
    v = seccion.get(clave) if isinstance(seccion, dict) else None
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v < minimo:
        raise ErrorConfig("%s.%s debe ser un numero >= %s (es %r)" % (donde, clave, minimo, v))


def validar(cfg: dict) -> None:
    if cfg.get("canal") not in CANALES:
        raise ErrorConfig("canal debe ser uno de %s (es %r)" % (CANALES, cfg.get("canal")))

    seg = cfg.get("seguimiento")
    if not isinstance(seg, dict):
        raise ErrorConfig("Falta la seccion seguimiento")
    intervalos = seg.get("intervalo_min_por_fuente", {})
    for fuente in FUENTES:
        _num(intervalos, fuente, "seguimiento.intervalo_min_por_fuente", 15)
    _num(seg, "pausa_entre_amazon_seg", "seguimiento")
    _num(seg, "lectura_extra_min_entre", "seguimiento", 15)
    reglas = seg.get("reglas", {})
    for k in ("bajada_pct", "bajada_eur", "min_observaciones_minimo", "min_dias_minimo",
              "historial_caducidad_dias"):
        _num(reglas, k, "seguimiento.reglas")
    _num(seg, "fallos_antes_de_aviso_tecnico", "seguimiento", 1)

    vistos = set()
    for i, p in enumerate(cfg.get("productos", [])):
        donde = "productos[%d]" % i
        if not isinstance(p, dict) or not p.get("nombre"):
            raise ErrorConfig("%s necesita al menos un nombre" % donde)
        fuente = p.get("fuente", "amazon_ficha")
        if fuente not in FUENTES:
            raise ErrorConfig("%s.fuente debe ser una de %s" % (donde, FUENTES))
        if fuente == "amazon_ficha":
            try:
                clave = asin_desde(p.get("asin", ""))
            except ValueError as exc:
                raise ErrorConfig("%s: %s" % (donde, exc)) from exc
        else:
            if not p.get("url") or not p.get("variant_id"):
                raise ErrorConfig("%s (shopify) necesita url y variant_id" % donde)
            clave = "shopify:%s" % p["variant_id"]
        if clave in vistos:
            raise ErrorConfig("%s esta repetido (%s)" % (donde, clave))
        vistos.add(clave)
        for k in ("precio_objetivo", "bajada_pct", "bajada_eur"):
            if p.get(k) is not None:
                _num(p, k, donde)

    des = cfg.get("descubrimiento")
    if not isinstance(des, dict):
        raise ErrorConfig("Falta la seccion descubrimiento")
    fuentes = des.get("fuentes")
    if not isinstance(fuentes, dict):
        raise ErrorConfig("descubrimiento.fuentes debe ser un objeto")
    for nombre, f in fuentes.items():
        if nombre not in FUENTES_DESCUBRIMIENTO:
            raise ErrorConfig("descubrimiento.fuentes.%s no existe. Fuentes: %s"
                              % (nombre, ", ".join(FUENTES_DESCUBRIMIENTO)))
        _num(f, "intervalo_min", "descubrimiento.fuentes.%s" % nombre, 60 if nombre == "chollomazon" else 1)
        _num(f, "max_antiguedad_horas", "descubrimiento.fuentes.%s" % nombre, 1)
    if des.get("activo") and not any(f.get("activo") for f in fuentes.values()):
        raise ErrorConfig("descubrimiento.activo es true pero no hay ninguna fuente activa")
    for k in ("precio_min", "precio_max", "valoracion_min", "valoraciones_min", "max_avisos_ejecucion",
              "max_avisos_dia", "max_verificaciones_dia"):
        _num(des, k, "descubrimiento")
    _num(des.get("oferta_interesante"), "descuento_min_pct", "descubrimiento.oferta_interesante")
    _num(des.get("oferta_interesante"), "ahorro_min_eur", "descubrimiento.oferta_interesante")
    _num(des.get("minimo_local"), "min_observaciones", "descubrimiento.minimo_local", 2)
    _num(des.get("minimo_local"), "min_dias", "descubrimiento.minimo_local", 1)
    if des["precio_min"] > des["precio_max"]:
        raise ErrorConfig("descubrimiento.precio_min es mayor que precio_max")
    for k in ("categorias", "excluir_categorias", "marcas", "palabras_clave", "excluir_palabras"):
        if not isinstance(des.get(k, []), list):
            raise ErrorConfig("descubrimiento.%s debe ser una lista" % k)


def productos(cfg: dict) -> list[Producto]:
    lista = []
    for p in cfg.get("productos", []):
        fuente = p.get("fuente", "amazon_ficha")
        objetivo = a_centimos(p["precio_objetivo"]) if p.get("precio_objetivo") is not None else None
        if fuente == "amazon_ficha":
            asin = asin_desde(p["asin"])
            lista.append(Producto(asin, p["nombre"], fuente, url_amazon(asin),
                                  p.get("categoria", "Otros"), objetivo,
                                  p.get("bajada_pct"), p.get("bajada_eur")))
        else:
            lista.append(Producto("shopify:%s" % p["variant_id"], p["nombre"], fuente, p["url"],
                                  p.get("categoria", "Otros"), objetivo, p.get("bajada_pct"),
                                  p.get("bajada_eur"), {"variant_id": str(p["variant_id"]),
                                                         "tienda": p.get("tienda", "Tienda")}))
    return lista


def anadir_producto(cfg: dict, asin_o_url: str, nombre: str | None, categoria: str | None,
                    objetivo: float | None) -> tuple[dict, str]:
    """Devuelve (config nueva, asin). Error si el ASIN no vale o ya esta."""
    asin = asin_desde(asin_o_url)
    if any(p.get("asin", "").upper() == asin for p in cfg.get("productos", [])):
        raise ErrorConfig("%s ya esta en la lista" % asin)
    nuevo = copy.deepcopy(cfg)
    entrada = {"asin": asin, "nombre": nombre or asin, "categoria": categoria or "Otros"}
    if objetivo is not None:
        entrada["precio_objetivo"] = objetivo
    nuevo.setdefault("productos", []).append(entrada)
    validar(nuevo)
    return nuevo, asin


def quitar_producto(cfg: dict, asin_o_url: str) -> dict:
    asin = asin_desde(asin_o_url)
    nuevo = copy.deepcopy(cfg)
    antes = len(nuevo.get("productos", []))
    nuevo["productos"] = [p for p in nuevo.get("productos", []) if p.get("asin", "").upper() != asin]
    if len(nuevo["productos"]) == antes:
        raise ErrorConfig("%s no esta en la lista" % asin)
    return nuevo
