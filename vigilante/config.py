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
VERSION = 8
CANALES = ("telegram", "discord", "consola")
FUENTES = ("amazon_ficha", "jsonld", "shopify", "pccomtracker")
# Tiendas que no dejan leer sus fichas de forma automatica (comprobado el
# 21/11/2026): van como enlace en las tarjetas, nunca como tienda leida.
NO_LEIBLES = {"pccomponentes.com": "PcComponentes no deja leer sus fichas al bot (Cloudflare, HTTP 403): "
                                   "anade 'pccomtracker' con la URL de su ficha en pccomtracker.com, "
                                   "o ponla en 'enlaces'"}
# v7: PcComponentes pasa de enlace a tienda seguida, leida en pccomtracker.com
# (fichas comprobadas el 21/11/2026: misma URL de PcComponentes en su JSON-LD).
PCCOMTRACKER_V7 = {
    "https://www.pccomponentes.com/xiaomi-vacuum-cleaner-g20-aspiradora-escoba-mano-sin-cable-460w-beige":
        "https://pccomtracker.com/producto/xiaomi-vacuum-cleaner-g20-aspiradora-escobamano-sin-cable-460w-beige-71159",
    "https://www.pccomponentes.com/dreame-u20-aspiradora-escoba-mano-sin-cable-370w-blanca":
        "https://pccomtracker.com/producto/dreame-u20-aspiradora-escobamano-sin-cable-370w-blanca-20128212",
}

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
# Temas de interes por defecto (nombres reales de cada fuente, 21/11/2026).
INTERESES_CHOLLOMETRO = ["Electrónica", "Gaming", "Hogar, vivienda y oficina"]
INTERESES_CHOLLOMAZON = ["Electrónica", "Informática", "Gaming", "Hogar", "Herramientas"]

# Palabras para reconocer cada producto en los titulos de Chollometro. Las
# exclusiones evitan confundir modelos parecidos (G20 Lite, G20 Max...).
EN_COMUNIDAD = {
    "B0DJ82JKFL": {"incluir": ["xiaomi g20", "vacuum cleaner g20"], "excluir": ["lite", "max"]},
    "dreame-u20": {"incluir": ["dreame u20"], "excluir": []},
    "dreame-r20-se-a": {"incluir": ["r20 se-a", "r20 sea", "r20 se a"], "excluir": []},
}
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
# v8: cuatro niveles. Nivel 2, categorias de interes activo (ejemplo: lo que
# buscas ahora). Nivel 4, errores de precio en cualquier categoria.
INTERESES_V8 = [{
    "nombre": "Aspiradoras", "activo": True,
    "incluir": ["aspiradora", "aspirador", "vacuum cleaner"],
    "excluir": ["robot", "recambio", "repuesto", "bolsas", "batería", "bateria", "soporte", "mopa",
                "coche", "piscina", "cenizas", "industrial", "taller"],
    "precio_min": 50, "precio_max": 250, "precio_objetivo": 140,
    "descuento_min_pct": 15, "ahorro_min_eur": 10,
}]
ERRORES_V8 = {"descuento_min_pct": 80, "ahorro_min_eur": 30, "valor_min_eur": 50, "todas_las_categorias": True}
FUENTES_DESCUBRIMIENTO = ("chollomazon", "chollometro", "simulado")
MIN_INTERVALO_FUENTE = {"chollomazon": 60, "chollometro": 5}


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
    if cfg.get("version", 1) < 5:
        # v5: el seguimiento avisa de cualquier subida o bajada, y el
        # descubrimiento busca chollos de verdad (70 % o mas, errores de precio).
        reglas = cfg.setdefault("seguimiento", {}).setdefault("reglas", {})
        reglas.setdefault("avisar_cambios", True)
        reglas.setdefault("cambio_min_eur", 1)
        cambios.append("el seguimiento avisa de cada bajada o subida de 1 EUR o mas")
        for p in cfg.get("productos", []):
            if (p.get("id"), p.get("precio_objetivo")) in (("dreame-u20", 100.0), ("dreame-r20-se-a", 140.0)):
                p.pop("precio_objetivo")
                cambios.append("quitado el objetivo por defecto de la %s" % p["nombre"])
        des = cfg.setdefault("descubrimiento", {})
        oi = des.setdefault("oferta_interesante", {})
        if oi.get("descuento_min_pct", 20) < 70:
            oi["descuento_min_pct"] = 70
            cambios.append("solo ofertas con 70 % de descuento o mas")
        if des.get("categorias") == ["Electrónica", "Informática", "Hogar"]:
            des["categorias"] = []
        if des.get("precio_min") == 25:
            des["precio_min"] = 5
        des.setdefault("fuentes", {}).setdefault("chollometro", copy.deepcopy(ejemplo()["descubrimiento"]["fuentes"]["chollometro"]))
        cambios.append("anadidos los chollos y errores de precio de Chollometro (cada 5 min)")
        cfg["version"] = 5
    if cfg.get("version", 1) < 6:
        # v6: fuera los chollos de calderilla y los falsos "errores de precio".
        des = cfg.setdefault("descubrimiento", {})
        com = des.setdefault("comunidad", {})
        palabras = com.get("palabras_error", [])
        com["palabras_error"] = [k for k in palabras if k not in ("preciazo", "precio de risa")]
        if com.get("ahorro_min_eur", 10) < 30:
            com["ahorro_min_eur"] = 30
        com.setdefault("valor_min_eur", 50)
        com.setdefault("categorias", list(INTERESES_CHOLLOMETRO))
        if not des.get("categorias"):
            des["categorias"] = list(INTERESES_CHOLLOMAZON)
        cambios.append("los chollos se filtran por tus temas: tecnologia, informatica, gaming y hogar")
        com.setdefault("precio_min_error_eur", 10)
        oi = des.setdefault("oferta_interesante", {})
        if oi.get("ahorro_min_eur", 10) < 30:
            oi["ahorro_min_eur"] = 30
        cambios.append("chollos con al menos 30 EUR de ahorro sobre productos de 50 EUR o mas; "
                       "errores de precio solo si lo dice el titulo")
        for prod in cfg.get("productos", []):
            pid = prod.get("id") or prod.get("asin")
            if pid in EN_COMUNIDAD and "en_comunidad" not in prod:
                prod["en_comunidad"] = copy.deepcopy(EN_COMUNIDAD[pid])
                cambios.append("la %s se busca tambien en Chollometro" % prod["nombre"])
        intervalos = cfg.setdefault("seguimiento", {}).setdefault("intervalo_min_por_fuente", {})
        for lector in ("jsonld", "shopify"):
            if intervalos.get(lector) in (120, 360):
                intervalos[lector] = 30
        cambios.append("Powerplanet, MediaMarkt y Dreame se leen cada 30 min")
        cfg["version"] = 6
    if cfg.get("version", 1) < 7:
        # v7: PcComponentes se sigue de verdad (via pccomtracker) y se guardan
        # los historicos de reacofertas y pccomtracker.
        for prod in cfg.get("productos", []):
            quedan = []
            for e in prod.get("enlaces", []):
                pct = PCCOMTRACKER_V7.get(e.get("url", "").rstrip("/"))
                if pct and prod.get("tiendas") is not None:
                    prod["tiendas"].append({"tienda": "PcComponentes", "url": e["url"], "pccomtracker": pct})
                    cambios.append("la %s se sigue tambien en PcComponentes" % prod.get("nombre"))
                else:
                    quedan.append(e)
            if "enlaces" in prod:
                prod["enlaces"] = quedan
        intervalos = cfg.setdefault("seguimiento", {}).setdefault("intervalo_min_por_fuente", {})
        intervalos.setdefault("pccomtracker", 60)
        cfg["version"] = 7
    if cfg.get("version", 1) < 8:
        # v8: cuatro niveles de seguimiento.
        reg = cfg.setdefault("seguimiento", {}).setdefault("reglas", {})
        reg["avisar_cambios"] = True
        if reg.get("cambio_min_eur", 1) >= 1:
            reg["cambio_min_eur"] = 0.01
        reg.setdefault("avisar_cambios_por_tienda", True)
        cambios.append("tus productos avisan de cualquier cambio de precio (desde 0,01 EUR) o de stock "
                       "en cada tienda")
        des = cfg.setdefault("descubrimiento", {})
        des.setdefault("intereses", copy.deepcopy(INTERESES_V8))
        cambios.append("nueva lista de intereses activos: aspiradoras (15 % o hasta 140 EUR)")
        for sec in (des.setdefault("oferta_interesante", {}), des.setdefault("comunidad", {})):
            if sec.get("descuento_min_pct", 70) >= 70:
                sec["descuento_min_pct"] = 60
        cambios.append("chollazos desde el 60 % y 30 EUR de ahorro")
        des.setdefault("errores", dict(ERRORES_V8))
        cambios.append("posibles errores de precio desde el 80 %, en cualquier categoria")
        cfg["version"] = 8
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
        ec = p.get("en_comunidad")
        if ec is not None and not (isinstance(ec, dict) and isinstance(ec.get("incluir", []), list)
                                   and isinstance(ec.get("excluir", []), list)):
            raise ErrorConfig("%s.en_comunidad debe tener listas 'incluir' y 'excluir'" % donde)
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
        _num(f, "intervalo_min", "descubrimiento.fuentes.%s" % nombre, MIN_INTERVALO_FUENTE.get(nombre, 1))
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
    err = des.get("errores")
    for k in ("descuento_min_pct", "ahorro_min_eur", "valor_min_eur"):
        _num(err, k, "descubrimiento.errores")
    intereses = des.get("intereses")
    if not isinstance(intereses, list):
        raise ErrorConfig("descubrimiento.intereses debe ser una lista")
    for i, it in enumerate(intereses):
        donde = "descubrimiento.intereses[%d]" % i
        if not isinstance(it, dict) or not it.get("nombre") or not it.get("incluir") \
                or not isinstance(it.get("incluir"), list) or not isinstance(it.get("excluir", []), list):
            raise ErrorConfig("%s necesita 'nombre' y una lista 'incluir' (y 'excluir' opcional)" % donde)
        _num(it, "descuento_min_pct", donde)
        for k in ("precio_min", "precio_max", "precio_objetivo", "ahorro_min_eur"):
            if it.get(k) is not None:
                _num(it, k, donde)
    com = des.get("comunidad")
    if not isinstance(com, dict):
        raise ErrorConfig("Falta la seccion descubrimiento.comunidad")
    _num(com, "descuento_min_pct", "descubrimiento.comunidad")
    _num(com, "ahorro_min_eur", "descubrimiento.comunidad")
    _num(com, "valor_min_eur", "descubrimiento.comunidad")
    _num(com, "precio_min_error_eur", "descubrimiento.comunidad")
    for k in ("palabras_error", "tiendas", "categorias"):
        if not isinstance(com.get(k), list):
            raise ErrorConfig("descubrimiento.comunidad.%s debe ser una lista" % k)
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
    if t.get("pccomtracker"):
        pct = t["pccomtracker"]
        if not pct.startswith("https://pccomtracker.com/producto/"):
            raise ValueError("'pccomtracker' de %s debe ser una ficha https://pccomtracker.com/producto/..." % nombre)
        return Tienda(nombre, "pccomtracker", "%s:%s" % (re.sub(r"\W+", "", nombre.lower()), _referencia(url)),
                      url, {"pccomtracker": pct})
    for dominio, motivo in NO_LEIBLES.items():
        if host.endswith(dominio):
            raise ValueError(motivo)
    lector = t.get("lector") or ("shopify" if re.search(r"/products/[^/]+/?$", urlsplit(url).path) else "jsonld")
    if lector not in FUENTES or lector in ("amazon_ficha", "pccomtracker"):
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
