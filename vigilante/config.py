"""Configuracion en un JSON editable sin tocar codigo (data/config.json).

Si no existe, se crea a partir de config.example.json. Se valida entera al
arrancar: un error de configuracion detiene el bot con un mensaje claro en vez
de fallar a mitad de un ciclo. Los secretos (tokens, webhook) van en variables
de entorno, nunca aqui.
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
CANALES = ("telegram", "discord", "consola")
PROVEEDORES_DESCUBRIMIENTO = ("ninguno", "simulado")
FUENTES = ("amazon_ficha", "shopify")


class ErrorConfig(ValueError):
    pass


def ruta_config() -> Path:
    return Path(os.environ.get("CONFIG_FILE", RAIZ / "data" / "config.json"))


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
    validar(cfg)
    return cfg


def guardar(cfg: dict, ruta: Path | None = None) -> None:
    validar(cfg)
    ruta = ruta or ruta_config()
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(ruta)


def _num(seccion: dict, clave: str, donde: str, minimo: float = 0) -> None:
    v = seccion.get(clave)
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
    if des.get("proveedor") not in PROVEEDORES_DESCUBRIMIENTO:
        raise ErrorConfig("descubrimiento.proveedor debe ser uno de %s. Las fuentes reales "
                          "pendientes de autorizacion estan explicadas en el README."
                          % (PROVEEDORES_DESCUBRIMIENTO,))
    if des.get("activo") and des.get("proveedor") == "ninguno":
        raise ErrorConfig("descubrimiento.activo es true pero no hay proveedor")
    if des.get("modo_minimo") not in ("externo", "local"):
        raise ErrorConfig("descubrimiento.modo_minimo debe ser 'externo' o 'local'")
    for k in ("intervalo_min", "precio_min", "precio_max", "descuento_min_pct", "ahorro_min_eur",
              "valoracion_min", "valoraciones_min", "dias_historial_min", "max_avisos_ejecucion",
              "max_avisos_dia", "max_candidatos"):
        _num(des, k, "descubrimiento")
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
