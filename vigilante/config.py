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
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from .modelo import Producto, Tienda, a_centimos, asin_desde, url_amazon

RAIZ = Path(__file__).resolve().parent.parent
EJEMPLO = RAIZ / "config.example.json"
VERSION = 4
CANALES = ("telegram", "discord", "consola")
FUENTES = ("amazon_ficha", "jsonld", "shopify")
# Tiendas que no dejan leer sus fichas de forma automatica (comprobado el
# 21/11/2026): van como enlace en las tarjetas, nunca como tienda leida.
NO_LEIBLES = {"pccomponentes.com": "PcComponentes sirve un desafio de Cloudflare (HTTP 403) "
                                   "incluso a un navegador real; ponla en 'enlaces'"}

# v4: la G20 pasa a seguirse en varias tiendas y se anaden la U20 y la R20 SE-A.
# Fichas y EAN comprobados el 21/11/2026.
G20_TIENDAS = [
    {"tienda": "Amazon", "asin": "B0DJ82JKFL"},
    {"tienda": "Powerplanet", "url": "https://www.powerplanetonline.com/es/xiaomi-vacuum-cleaner-g20-blanco"},
    {"tienda": "MediaMarkt", "url": "https://www.mediamarkt.es/es/product/"
                                    "_aspirador-escoba-xiaomi-g20-460-w-60-min-blanco-148640073.html"},
]
G20_ENLACES = [
    {"tienda": "PcComponentes", "url": "https://www.pccomponentes.com/"
                                       "xiaomi-vacuum-cleaner-g20-aspiradora-escoba-mano-sin-cable-460w-beige",
     "nota": "color beige"},
    {"tienda": "Xiaomi", "url": "https://www.mi.com/es/product/xiaomi-vacuum-cleaner-g20/"},
]
PRODUCTOS_V4 = [
    {"id": "dreame-u20", "nombre": "Dreame U20", "categoria": "Aspiradoras", "precio_objetivo": 100.0,
     "ean": "6976233670607",
     "tiendas": [
         {"tienda": "Dreame", "url": "https://es.dreametech.com/products/aspirador-sin-cable-u20"},
         {"tienda": "MediaMarkt", "url": "https://www.mediamarkt.es/es/product/"
                                         "_aspirador-escoba-dreame-308668681-370-w-60-min-negro-139804042.html"},
     ],
     "enlaces": [{"tienda": "PcComponentes",
                  "url": "https://www.pccomponentes.com/dreame-u20-aspiradora-escoba-mano-sin-cable-370w-blanca"}]},
    {"id": "dreame-r20-se-a", "nombre": "Dreame R20 SE-A", "categoria": "Aspiradoras", "precio_objetivo": 140.0,
     "ean": "6978515257575",
     "tiendas": [
         {"tienda": "Amazon", "asin": "B0FY784L7H"},
         {"tienda": "Dreame", "url": "https://es.dreametech.com/products/aspirador-sin-cable-r20-se-a"},
     ]},
]
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
    if cfg.get("version", 1) < 3:
        # v3: un stock que dura poco no se puede vigilar cada 12 h. Solo se
        # cambia si sigue el valor por defecto anterior.
        intervalos = cfg.get("seguimiento", {}).get("intervalo_min_por_fuente", {})
        if intervalos.get("amazon_ficha") == 720:
            intervalos["amazon_ficha"] = 120
            cambios.append("productos de Amazon cada 2 h en lugar de cada 12 h")
        cfg["version"] = 3
    if cfg.get("version", 1) < 4:
        productos_cfg = cfg.setdefault("productos", [])
        for p in productos_cfg:
            if p.get("asin", "").upper() == "B0DJ82JKFL" and "tiendas" not in p:
                asin = p.pop("asin")
                p.update(id=asin, ean="6941812791936", tiendas=copy.deepcopy(G20_TIENDAS),
                         enlaces=copy.deepcopy(G20_ENLACES))
                cambios.append("la G20 se sigue tambien en Powerplanet y MediaMarkt")
        ids = {p.get("id") or p.get("asin") for p in productos_cfg}
        for nuevo in PRODUCTOS_V4:
            if nuevo["id"] not in ids:
                productos_cfg.append(copy.deepcopy(nuevo))
                cambios.append("anadida la %s" % nuevo["nombre"])
        cfg["version"] = 4
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
        try:
            producto = _producto(p)
        except (ValueError, KeyError) as exc:
            raise ErrorConfig("%s: %s" % (donde, exc)) from exc
        claves = [producto.clave] + [t.clave for t in producto.tiendas if t.clave != producto.clave]
        for clave in claves:
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


def _referencia(url: str) -> str:
    ruta = urlsplit(url).path.rstrip("/")
    return re.sub(r"\.(html?|js)$", "", ruta.rsplit("/", 1)[-1]) or urlsplit(url).netloc


def _tienda(t: dict) -> Tienda:
    nombre = (t.get("tienda") or "").strip()
    if not nombre:
        raise ValueError("cada tienda necesita un nombre ('tienda')")
    if t.get("asin"):
        asin = asin_desde(t["asin"])
        return Tienda(nombre, "amazon_ficha", asin, url_amazon(asin))
    url = t.get("url") or ""
    if not url.startswith("https://"):
        raise ValueError("la tienda %s necesita 'asin' o una 'url' https" % nombre)
    host = urlsplit(url).netloc.lower()
    for dominio, motivo in NO_LEIBLES.items():
        if host.endswith(dominio):
            raise ValueError(motivo)
    lector = t.get("lector") or ("shopify" if re.search(r"/products/[^/]+/?$", urlsplit(url).path) else "jsonld")
    if lector not in FUENTES or lector == "amazon_ficha":
        raise ValueError("lector no valido para %s: %s" % (nombre, lector))
    clave = "%s:%s" % (re.sub(r"\W+", "", nombre.lower()), t.get("variant_id") or _referencia(url))
    extra = {"variant_id": str(t["variant_id"])} if t.get("variant_id") else {}
    return Tienda(nombre, lector, clave, url, extra)


def _producto(p: dict) -> Producto:
    objetivo = a_centimos(p["precio_objetivo"]) if p.get("precio_objetivo") is not None else None
    comun = dict(nombre=p["nombre"], categoria=p.get("categoria", "Otros"), precio_objetivo_cent=objetivo,
                 bajada_pct=p.get("bajada_pct"), bajada_eur=p.get("bajada_eur"))
    if p.get("tiendas"):
        if not p.get("id"):
            raise ValueError("un producto con varias tiendas necesita un 'id'")
        tiendas = [_tienda(t) for t in p["tiendas"]]
        enlaces = [(e["tienda"], e["url"], e.get("nota", "")) for e in p.get("enlaces", [])]
        ean = str(p["ean"]) if p.get("ean") else None
        return Producto(clave=str(p["id"]), ean=ean, tiendas=tiendas, enlaces=enlaces, **comun)
    if p.get("fuente") == "shopify":  # formato antiguo
        if not p.get("url") or not p.get("variant_id"):
            raise ValueError("(shopify) necesita url y variant_id")
        t = Tienda(p.get("tienda", "Tienda"), "shopify", "shopify:%s" % p["variant_id"], p["url"],
                   {"variant_id": str(p["variant_id"])})
        return Producto(clave=t.clave, tiendas=[t], **comun)
    asin = asin_desde(p.get("asin", ""))  # formato antiguo: solo Amazon
    return Producto(clave=asin, tiendas=[Tienda("Amazon", "amazon_ficha", asin, url_amazon(asin))], **comun)


def productos(cfg: dict) -> list[Producto]:
    return [_producto(p) for p in cfg.get("productos", [])]


def anadir_producto(cfg: dict, asin_o_url: str, nombre: str | None, categoria: str | None,
                    objetivo: float | None) -> tuple[dict, str]:
    """Devuelve (config nueva, asin). Error si el ASIN no vale o ya esta."""
    asin = asin_desde(asin_o_url)
    if any(asin in [pr.clave] + [t.clave for t in pr.tiendas] for pr in productos(cfg)):
        raise ErrorConfig("%s ya esta en la lista" % asin)
    nuevo = copy.deepcopy(cfg)
    entrada = {"asin": asin, "nombre": nombre or asin, "categoria": categoria or "Otros"}
    if objetivo is not None:
        entrada["precio_objetivo"] = objetivo
    nuevo.setdefault("productos", []).append(entrada)
    validar(nuevo)
    return nuevo, asin


def quitar_producto(cfg: dict, id_asin_o_url: str) -> dict:
    """Quita por id de producto, ASIN o URL de Amazon."""
    try:
        clave = asin_desde(id_asin_o_url)
    except ValueError:
        clave = id_asin_o_url.strip()
    nuevo = copy.deepcopy(cfg)
    antes = len(nuevo.get("productos", []))
    nuevo["productos"] = [p for p in nuevo.get("productos", [])
                          if clave not in [_producto(p).clave] + [t.clave for t in _producto(p).tiendas]]
    if len(nuevo["productos"]) == antes:
        raise ErrorConfig("%s no esta en la lista" % asin)
    return nuevo
