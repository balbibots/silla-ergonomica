"""Modo 2: descubrir ofertas por categorias, sin intervencion manual.

Niveles (el 1, tus productos, esta en seguimiento.py):

- Nivel 2 · intereses activos (🛍️): lo que buscas ahora (p. ej. aspiradoras),
  por palabras del titulo y con filtros flexibles: un descuento modesto
  (15 %) o un precio por debajo de tu objetivo. En cualquier categoria.
- Nivel 3 · chollazos (💥): descuento REAL de al menos el 60 % y 30 EUR, en
  tus temas. Real quiere decir frente a precios que se han cobrado: el precio
  habitual de pccomtracker, la mediana de 90 dias de reacofertas o el que el
  bot ha visto durante al menos un mes. Un tachado, un PVPR o el "antes" de
  una publicacion nunca bastan por si solos.
- Nivel 4 · errores de precio (🚨): 80 % o mas (o la comunidad lo marca asi en
  el titulo), en CUALQUIER categoria y a cualquier hora. "Sin comprobar" si el
  bot no ha visto ese precio en la tienda; "comprobado" si lo ha leido en la
  ficha (hoy, Amazon).

Una oferta que encaja en varios niveles sale una sola vez, con el mas fuerte
(error > minimos > chollazo > interes) y una nota si ademas es de tu lista.
Nunca se avisa de algo reacondicionado, de segunda mano o usado salvo que
desactives solo_nuevo.

Flujo de cada ejecucion (cada fuente a su ritmo, p. ej. CholloMazon cada hora):

1. Descarga las ofertas de la fuente y guarda su precio como historico LOCAL
   de esa fuente (solo si cambia o ha pasado un dia, para no inflar la base).
2. Si un producto que sigues en el modo 1 aparece mas barato, lo marca para
   leerlo en Amazon en la siguiente vuelta (lectura extra).
3. Filtra por categoria, precio, marca, palabras y antiguedad del dato.
4. Clasifica cada candidato en UNO de tres niveles, que no se mezclan:
   - minimo_externo   🏆 por debajo del minimo de 365 d de Amazon importado
   - minimo_local     📊 por debajo de todo lo que el bot ha visto de el
                         (con observaciones y dias suficientes)
   - oferta           🔥 descuento frente al precio de REFERENCIA de Amazon
                         (no es un historico, y la tarjeta lo dice)
5. Ordena (nivel y ahorro), comprueba en la ficha de Amazon los finalistas
   (precio, unidades nuevas, valoraciones; con limite diario) y avisa de los
   mejores, con limites por ejecucion y por dia y sin repetir nunca el mismo
   producto al mismo precio.

Las ofertas que ya estan activas cuentan desde la primera descarga: se avisa
de las mejores, dentro de los limites, y nunca dos veces al mismo precio.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import historial_amazon
from .almacen import Almacen
from .fuentes import Candidato, FuenteNoDisponible, SinNovedades
from .modelo import NUEVO, Observacion, eur, fecha_corta, hora_madrid, iso, desde_iso
from .tarjeta import Tarjeta

MODO = "descubrimiento"
NIVELES = ["error", "minimo_externo", "minimo_local", "oferta", "interes"]
# En el titulo: el producto no es nuevo (Amazon Renewed, Warehouse...).
NO_NUEVO = ("reacondicionad", "refurbished", "renewed", "segunda mano", "seminuevo", "usado", "usada",
            "open box", "outlet", "warehouse", "como nuevo", "reparado")


def no_es_nuevo(titulo: str) -> bool:
    t = titulo.lower()
    return any(k in t for k in NO_NUEVO)


def descuento(precio: int | None, antes: int | None) -> tuple[int, float]:
    """(ahorro en centimos, porcentaje) frente al precio anterior, o (0, 0)."""
    if precio is None or not antes or antes <= precio:
        return 0, 0.0
    return antes - precio, (antes - precio) * 100 / antes


def interes_para(titulo: str, precio: int | None, antes: int | None, des: dict,
                 catalogo: bool = False) -> dict | None:
    """El interes activo (nivel 2) en el que encaja: palabras del titulo, rango
    de precio y, o descuento suficiente, o precio por debajo del objetivo. Lo
    que sale de un catalogo (una categoria o una busqueda, no una oferta)
    necesita siempre descuento: estar por debajo del objetivo no basta."""
    if precio is None:
        return None
    t = titulo.lower()
    for i in des.get("intereses", []):
        if not i.get("activo", True):
            continue
        if not any(k.lower() in t for k in i["incluir"]) or any(k.lower() in t for k in i.get("excluir", [])):
            continue
        if precio < (i.get("precio_min") or 0) * 100:
            continue
        if i.get("precio_max") is not None and precio > i["precio_max"] * 100:
            continue
        ahorro, pct = descuento(precio, antes)
        rebajado = pct >= i["descuento_min_pct"] and ahorro >= (i.get("ahorro_min_eur") or 0) * 100
        objetivo = i.get("precio_objetivo")
        if catalogo:
            if rebajado and (objetivo is None or precio <= objetivo * 100):
                return i
        elif rebajado or (objetivo is not None and precio <= objetivo * 100):
            return i
    return None


def es_error(precio: int | None, antes: int | None, err: dict) -> bool:
    """Nivel 4 por numeros: 80 % o mas sobre algo que valia al menos valor_min
    (y no mas de max_referencia: un "antes" de 12.000 EUR en un adaptador es un
    dato roto, no un error de precio)."""
    ahorro, pct = descuento(precio, antes)
    return bool(antes) and err["valor_min_eur"] * 100 <= antes <= err.get("max_referencia_eur", 10 ** 6) * 100 \
        and err["descuento_min_pct"] <= pct <= err.get("max_descuento_pct", 100) \
        and ahorro >= err["ahorro_min_eur"] * 100


def es_chollazo(precio: int | None, ref_real: int | None, sec: dict, valor_min: float,
                err: dict | None = None) -> bool:
    """Nivel 3: solo frente a una referencia REAL (historico), y que no sea un
    dato roto: una referencia por encima de max_referencia_eur o un descuento
    por encima de max_descuento_pct (una tapa de objetivo "a 1.227 EUR")."""
    err = err or {}
    ahorro, pct = descuento(precio, ref_real)
    return bool(ref_real) and valor_min * 100 <= ref_real <= err.get("max_referencia_eur", 10 ** 6) * 100 \
        and sec["descuento_min_pct"] <= pct <= err.get("max_descuento_pct", 100) \
        and ahorro >= sec["ahorro_min_eur"] * 100


def referencia_local(almacen: Almacen, asin: str, ahora: datetime, min_dias: int = 30,
                     min_obs: int = 5) -> int | None:
    """Precio habitual que el bot ha visto de un ASIN (mediana de los ultimos 90
    dias, en cualquier fuente), si lleva al menos un mes viendolo."""
    filas = almacen.db.execute(
        "SELECT fecha, precio_cent FROM observaciones WHERE clave=? AND condicion='nuevo' AND disponible=1"
        " AND precio_cent IS NOT NULL AND fecha>=? AND fecha<?",
        (asin, iso(ahora - timedelta(days=90)), iso(ahora))).fetchall()
    if len(filas) < min_obs:
        return None
    fechas = sorted(desde_iso(f["fecha"]) for f in filas)
    if (fechas[-1] - fechas[0]).days < min_dias:
        return None
    precios = sorted(f["precio_cent"] for f in filas)
    return precios[len(precios) // 2]


@dataclass
class Hallazgo:
    cand: Candidato
    nivel: str
    ahorro_cent: int = 0
    referencia_cent: int | None = None
    referencia_etiqueta: str = ""
    minimo_cent: int | None = None
    periodo: str = ""
    verificado: bool = False
    interes: str | None = None
    ref_real: bool = False      # la referencia sale de un historico, no de un tachado

    @property
    def puntuacion(self) -> tuple:
        calidad = (self.cand.valoracion or 4) / 5
        return (-NIVELES.index(self.nivel), self.ahorro_cent * calidad)


def tipo_precio(fuente: str) -> str:
    return "oferta_%s" % fuente


def _en_horario(des: dict, ahora: datetime) -> bool:
    h = des.get("horario")
    if not h:
        return True
    t = hora_madrid(ahora).strftime("%H:%M")
    return h["desde"] <= t < h["hasta"] if h["desde"] <= h["hasta"] else not (h["hasta"] <= t < h["desde"])


def _categoria_ok(categoria: str, des: dict) -> bool:
    principal = categoria.split(">")[0].strip().lower()
    completa = categoria.lower()
    if any(c.lower() in (principal, completa) for c in des["excluir_categorias"]):
        return False
    return not des["categorias"] or any(c.lower() in (principal, completa) for c in des["categorias"])


def pasa_filtros(c: Candidato, des: dict, max_antiguedad: timedelta, ahora: datetime) -> str | None:
    """None si pasa; si no, el motivo (para el log)."""
    titulo = c.titulo.lower()
    base = filtro_base(c, des, max_antiguedad, ahora)
    if base:
        return base
    if not _categoria_ok(c.categoria, des):
        return "categoria"
    if not des["precio_min"] * 100 <= c.precio_cent <= des["precio_max"] * 100:
        return "precio fuera de rango"
    if des["marcas"] and (c.marca or "").lower() not in [m.lower() for m in des["marcas"]]:
        return "marca"
    if des["palabras_clave"] and not any(k.lower() in titulo for k in des["palabras_clave"]):
        return "palabras clave"
    return None


def filtro_base(c: Candidato, des: dict, max_antiguedad: timedelta, ahora: datetime) -> str | None:
    """Lo que se aplica a todos los niveles: nuevo, con stock, dato reciente y
    sin palabras excluidas."""
    if des.get("solo_nuevo", True) and (c.condicion != NUEVO or not c.disponible or no_es_nuevo(c.titulo)):
        return "no es nuevo"
    if c.comprobada and ahora - c.comprobada > max_antiguedad:
        return "dato antiguo"
    if any(k.lower() in c.titulo.lower() for k in des["excluir_palabras"]):
        return "palabra excluida"
    if c.precio_cent < des["precio_min"] * 100:
        return "precio fuera de rango"
    return None


def clasificar_todo(c: Candidato, des: dict, almacen: Almacen, ahora: datetime,
                    max_antiguedad: timedelta) -> Hallazgo | None:
    """El nivel mas fuerte en el que encaja la oferta, o None."""
    if filtro_base(c, des, max_antiguedad, ahora):
        return None
    ref = c.precio_referencia_cent
    interes = interes_para(c.titulo, c.precio_cent, ref, des)
    h = None
    if es_error(c.precio_cent, ref, des["errores"]) and (des["errores"]["todas_las_categorias"]
                                                         or _categoria_ok(c.categoria, des)):
        h = Hallazgo(c, "error", ref - c.precio_cent, ref, "ref. Amazon")
    if h is None and pasa_filtros(c, des, max_antiguedad, ahora) is None:
        h = clasificar(c, des, almacen, ahora)
    if h is None and interes:
        ahorro, _ = descuento(c.precio_cent, ref)
        h = Hallazgo(c, "interes", ahorro, ref if ahorro else None, "ref. Amazon" if ahorro else "")
    if h is not None and interes:
        h.interes = interes["nombre"]
    return h


def _guardar_observacion(almacen: Almacen, c: Candidato, ahora: datetime) -> None:
    """Historico local de la fuente: una fila si el precio cambia o cada 24 h."""
    ultima = almacen.db.execute(
        "SELECT fecha, precio_cent FROM observaciones WHERE clave=? AND tipo_precio=?"
        " ORDER BY fecha DESC LIMIT 1", (c.asin, tipo_precio(c.fuente))).fetchone()
    if ultima and ultima["precio_cent"] == c.precio_cent and \
            ahora - desde_iso(ultima["fecha"]) < timedelta(hours=24):
        return
    almacen.registrar(Observacion(clave=c.asin, fecha=ahora, fuente=c.fuente, precio_cent=c.precio_cent,
                                  tipo_precio=tipo_precio(c.fuente), cupon_texto=c.cupon))


def _marcar_seguidos(almacen: Almacen, seguidos: set[str], c: Candidato) -> None:
    """Un producto seguido aparece en ofertas mas barato: lectura extra en Amazon."""
    if c.asin not in seguidos:
        return
    serie = almacen.serie(c.asin)
    if serie and c.precio_cent < serie[-1][1]:
        est = almacen.estado(c.asin)
        if not est.get("leer_ya"):
            est["leer_ya"] = True
            almacen.guardar_estado(c.asin, est)
            print("  %s aparece en %s a %s: se leera en Amazon enseguida" % (c.asin, c.fuente, eur(c.precio_cent)))


def clasificar(c: Candidato, des: dict, almacen: Almacen, ahora: datetime) -> Hallazgo | None:
    avisar = des["avisar"]
    precio = c.precio_cent

    # 1. Minimo externo verificado: grafico de 365 d de Amazon importado.
    ext = almacen.historial_externo(c.asin, historial_amazon.FUENTE)
    if avisar["minimo_externo"] and ext:
        captura, puntos = ext
        ventana = [p for f, p in puntos if f >= ahora - timedelta(days=365)]
        if ventana and ahora - captura <= timedelta(days=30) and precio < min(ventana):
            return Hallazgo(c, "minimo_externo", min(ventana) - precio, None, "", min(ventana), "365 d")

    # 2. Minimo del historico local (lo visto por el bot en esta fuente).
    ml = des["minimo_local"]
    previas = almacen.serie(c.asin, tipo_precio=tipo_precio(c.fuente), hasta=ahora)
    if avisar["minimo_local"] and len(previas) >= ml["min_observaciones"] \
            and (ahora - previas[0][0]).days >= ml["min_dias"]:
        minimo = min(p for _, p in previas)
        if precio < minimo:
            return Hallazgo(c, "minimo_local", minimo - precio, None, "", minimo,
                            "observado por el bot desde %s" % fecha_corta(previas[0][0])[:10])

    # 3. Chollazo: frente al precio habitual que el bot ha visto (un mes o
    # mas). El precio de referencia de Amazon (un tachado) no basta.
    oi = des["oferta_interesante"]
    real = referencia_local(almacen, c.asin, ahora)
    if avisar["ofertas_interesantes"] and es_chollazo(precio, real, oi, des["comunidad"]["valor_min_eur"],
                                                       des["errores"]):
        return Hallazgo(c, "oferta", real - precio, real, "habitual", ref_real=True)
    return None


def _con_valoraciones(h: Hallazgo, des: dict) -> bool:
    grupo = {"oferta": "chollo", "interes": "interes", "minimo_local": "minimo", "minimo_externo": "minimo"}
    return grupo.get(h.nivel) in des.get("valoraciones_en", [])


def _verificar(h: Hallazgo, des: dict, leer) -> str | None:
    """Comprueba el finalista en la ficha de Amazon (precio y unidades nuevas;
    valoraciones solo en los niveles de valoraciones_en, nunca en errores).
    None si vale; si no, el motivo."""
    try:
        obs = leer(h.cand.asin)
    except Exception as exc:  # noqa: BLE001
        return "no se pudo comprobar en Amazon (%s)" % type(exc).__name__
    if not obs.disponible or obs.precio_cent is None:
        return "sin unidades nuevas en Amazon"
    if obs.precio_cent > h.cand.precio_cent + 50:
        return "en Amazon ya cuesta %s" % eur(obs.precio_cent)
    if _con_valoraciones(h, des):
        if obs.valoracion is None or obs.valoracion < des["valoracion_min"]:
            return "valoracion %s" % obs.valoracion
        if (obs.num_valoraciones or 0) < des["valoraciones_min"]:
            return "pocas valoraciones (%s)" % obs.num_valoraciones
    bajada = h.cand.precio_cent - obs.precio_cent
    h.cand.precio_cent = obs.precio_cent
    h.ahorro_cent += max(bajada, 0)
    h.cand.valoracion, h.cand.num_valoraciones = obs.valoracion, obs.num_valoraciones
    h.cand.imagen, h.cand.vendedor = obs.imagen, obs.vendedor
    h.verificado = True
    return None


def _evento(h: Hallazgo) -> str:
    return "descubrimiento_%s" % h.nivel


def _ya_avisado(almacen: Almacen, clave: str, eventos, precio: int | None) -> bool:
    return any(almacen.aviso_enviado(clave, e, precio) for e in eventos)


def ejecutar(cfg: dict, almacen: Almacen, notificador, fuentes: list, ahora: datetime,
             leer=None, dormir=time.sleep, seguidos: set[str] | None = None, juez=None) -> int:
    """fuentes: objetos con nombre, atribucion y candidatos(). leer: lector de la
    ficha de Amazon para verificar (None = sin verificar). juez: busca la
    historia real de las publicaciones de Chollometro (juez.Juez)."""
    des = cfg["descubrimiento"]
    if not des["activo"]:
        return 0
    seguidos = seguidos or set()

    candidatos, comunidad, atribuciones = [], [], {}
    for fuente in fuentes:
        ajustes = des["fuentes"].get(fuente.nombre, {})
        if not ajustes.get("activo"):
            continue
        proxima = almacen.meta("proxima_%s" % fuente.nombre)
        if proxima and ahora < desde_iso(proxima):
            continue
        almacen.poner_meta("proxima_%s" % fuente.nombre, iso(ahora + timedelta(minutes=ajustes["intervalo_min"])))
        try:
            lote = fuente.candidatos()
        except SinNovedades:
            print("Descubrimiento: %s sin cambios" % fuente.nombre)
            continue
        except FuenteNoDisponible as exc:
            if exc.reintentar_tras:
                almacen.poner_meta("proxima_%s" % fuente.nombre, iso(ahora + exc.reintentar_tras))
            print("Descubrimiento: %s no disponible: %s" % (fuente.nombre, exc))
            continue
        except Exception as exc:  # noqa: BLE001 - una fuente no tumba al resto
            print("Descubrimiento: error leyendo %s: %s" % (fuente.nombre, exc))
            continue
        atribuciones[fuente.nombre] = fuente.atribucion
        max_ant = timedelta(hours=ajustes["max_antiguedad_horas"])
        if getattr(fuente, "comunidad", False):
            comunidad.extend((fuente, c, max_ant) for c in lote)
            print("Descubrimiento: %s trae %d chollos" % (fuente.nombre, len(lote)))
            continue
        for c in lote:
            _marcar_seguidos(almacen, seguidos, c)
            candidatos.append((c, max_ant))
        print("Descubrimiento: %s trae %d ofertas" % (fuente.nombre, len(lote)))

    hoy = hora_madrid(ahora).replace(hour=0, minute=0, second=0, microsecond=0)
    enviados = _procesar_comunidad(comunidad, cfg, almacen, notificador, ahora, hoy, atribuciones, juez)
    if not candidatos:
        return enviados
    hallazgos, vistos = [], set()
    for c, max_ant in candidatos:
        _guardar_observacion(almacen, c, ahora)
        if c.asin in vistos or c.asin in seguidos:
            continue  # un producto que sigues lo avisa el seguimiento, no el descubrimiento
        vistos.add(c.asin)
        h = clasificar_todo(c, des, almacen, ahora, max_ant)
        if h:
            hallazgos.append(h)

    if not _en_horario(des, ahora):
        # Un error de precio dura minutos: sale a cualquier hora.
        fuera = len(hallazgos)
        hallazgos = [h for h in hallazgos if h.nivel == "error"]
        print("Descubrimiento: %d hallazgos fuera de horario, %d errores de precio" % (fuera, len(hallazgos)))
    cupo = min(des["max_avisos_ejecucion"], des["max_avisos_dia"] - almacen.avisos_desde(MODO, hoy))
    verificaciones = des["max_verificaciones_dia"] - int(almacen.meta("verificaciones_%s" % hoy.date()) or 0)

    hallazgos.sort(key=lambda h: h.puntuacion, reverse=True)
    primera_lectura, enviados_antes = True, enviados
    eventos = ["descubrimiento_%s" % n for n in NIVELES]
    for h in hallazgos:
        if _ya_avisado(almacen, h.cand.asin, eventos, h.cand.precio_cent):
            continue
        # Los errores de precio no gastan el cupo diario, solo el de la ejecucion.
        tope = des["max_avisos_ejecucion"] if h.nivel == "error" else cupo
        if enviados - enviados_antes >= max(tope, 0):
            almacen.registrar_candidato(h.cand.asin, _resumen(h, "fuera de cupo"), notificado=False)
            continue
        if des["verificar_en_amazon"] and leer:
            if verificaciones <= 0 and h.nivel != "error":
                almacen.registrar_candidato(h.cand.asin, _resumen(h, "sin verificaciones hoy"), notificado=False)
                continue
            if verificaciones > 0:
                if not primera_lectura:
                    dormir(cfg["seguimiento"]["pausa_entre_amazon_seg"])
                primera_lectura = False
                verificaciones -= 1
                almacen.poner_meta("verificaciones_%s" % hoy.date(),
                                   str(des["max_verificaciones_dia"] - verificaciones))
                motivo = _verificar(h, des, leer)
                if motivo and not (h.nivel == "error" and motivo.startswith("no se pudo comprobar")):
                    almacen.registrar_candidato(h.cand.asin, _resumen(h, motivo), notificado=False)
                    print("  descartado %s: %s" % (h.cand.asin, motivo))
                    continue
            # Un error de precio que no se ha podido comprobar sale igual, como
            # "sin comprobar": suele durar minutos.
        notificador.enviar(tarjeta(h, ahora, atribuciones.get(h.cand.fuente, "")))
        almacen.registrar_aviso(h.cand.asin, _evento(h), h.cand.precio_cent, MODO, notificador.nombre, fecha=ahora)
        almacen.registrar_candidato(h.cand.asin, _resumen(h, "notificado"), notificado=True)
        enviados += 1
    print("Descubrimiento: %d hallazgos, %d avisos" % (len(hallazgos), enviados - enviados_antes))
    return enviados


# ------------------------------------------------------- chollos de la comunidad
def evaluar_chollo(c, des: dict, ahora: datetime, max_ant: timedelta) -> str | None:
    """'error_precio' (nivel 4), 'chollo' (nivel 3), 'interes' (nivel 2) o None.

    - Error de precio: la comunidad lo pone en el TITULO (en la descripcion sale
      en frases como "me parece un preciazo"), o el descuento frente al precio
      anterior de la publicacion es del 80 % o mas. En cualquier categoria.
    - Chollazo: 60 % y 30 EUR o mas sobre algo de 50 EUR o mas, en tus temas.
    - Interes: encaja en uno de tus intereses activos.
    Lo barato no interesa aunque sea un buen descuento: unos peines de 3 EUR o un
    polo de 15 EUR rebajado no merecen un aviso."""
    com, err = des["comunidad"], des["errores"]
    texto, titulo = c.texto.lower(), c.titulo.lower()
    if c.precio_cent is None or (c.publicada and ahora - c.publicada > max_ant):
        return None
    # Chollometro solo detecta: sin la historia real del producto no avisa.
    if c.fuente == "chollometro" and com.get("exigir_historico", True) and not c.ref_hist_cent:
        return None
    if any(k.lower() in texto for k in des["excluir_palabras"]):
        return None
    if des.get("solo_nuevo", True) and no_es_nuevo(c.titulo):
        return None
    if com["tiendas"] and c.tienda.lower() not in [t.lower() for t in com["tiendas"]]:
        return None
    en_temas = _en_temas(c, des)
    # La mejor referencia: la real (historico) o, si no hay, la declarada.
    ref = c.ref_hist_cent or c.precio_antes_cent
    ahorro, _ = descuento(c.precio_cent, ref)

    if err["todas_las_categorias"] or en_temas:
        if any(k.lower() in titulo for k in com["palabras_error"]):
            if ref and err["valor_min_eur"] * 100 <= ref <= err["max_referencia_eur"] * 100 \
                    and ahorro >= err["ahorro_min_eur"] * 100:
                return "error_precio"
            if not ref and c.precio_cent >= com["precio_min_error_eur"] * 100:
                return "error_precio"  # sin precio anterior: al menos que no sea calderilla
        elif es_error(c.precio_cent, c.ref_hist_cent, err) or (c.es_oferta and es_error(c.precio_cent,
                                                                                         c.precio_antes_cent, err)):
            return "error_precio"

    if en_temas and c.precio_cent <= des["precio_max"] * 100 \
            and (not des["palabras_clave"] or any(k.lower() in texto for k in des["palabras_clave"])):
        # Chollazo solo con referencia REAL: el "antes" de una publicacion no basta.
        if es_chollazo(c.precio_cent, c.ref_hist_cent, com, com["valor_min_eur"], err):
            return "chollo"
    if interes_para(c.titulo, c.precio_cent, _ref_interes(c), des, catalogo=not c.es_oferta):
        return "interes"
    return None


def _ref_interes(c) -> int | None:
    """Referencia para el nivel 2. Una oferta publicada puede usar su "antes";
    lo que sale de un catalogo (categoria o busqueda) necesita la real."""
    return c.ref_hist_cent if not c.es_oferta else (c.ref_hist_cent or c.precio_antes_cent)


def _en_temas(c, des: dict) -> bool:
    ajustes = des["fuentes"].get(c.fuente, {})
    temas = ajustes.get("categorias", des["comunidad"]["categorias"] if c.fuente == "chollometro" else [])
    if not temas:
        return True
    cat = (c.categoria or "").lower()
    principal = re.split(r"[›>]", cat)[0].strip()
    return any(k.lower() in (cat, principal) for k in temas)


def producto_en_chollo(c, productos: list[dict]) -> dict | None:
    """El producto seguido que aparece en el titulo de la publicacion, segun sus
    palabras 'en_comunidad' (incluir y excluir). Sin palabras, no se busca."""
    titulo = c.titulo.lower()
    for p in productos:
        reglas = p.get("en_comunidad") or {}
        incluir = [k.lower() for k in reglas.get("incluir", [])]
        excluir = [k.lower() for k in reglas.get("excluir", [])]
        if incluir and any(k in titulo for k in incluir) and not any(k in titulo for k in excluir):
            return p
    return None


def _procesar_comunidad(lote, cfg, almacen: Almacen, notificador, ahora, hoy, atribuciones, juez=None) -> int:
    des = cfg["descubrimiento"]
    enviados = 0
    for fuente, c, max_ant in lote:
        seguido = producto_en_chollo(c, cfg.get("productos", []))
        if seguido and c.fuente != "chollometro":
            continue  # tus productos en esas tiendas ya los lee el seguimiento
        if seguido and (not c.publicada or ahora - c.publicada <= max_ant):
            # Un producto que sigues, en cualquier tienda (tambien las que el bot
            # no puede leer, como PcComponentes): siempre interesa.
            clave = "%s:%s" % (fuente.nombre, c.id)
            if not almacen.aviso_enviado(clave, "comunidad_seguido", c.precio_cent):
                t = tarjeta_chollo(c, "visto_comunidad", ahora, atribuciones.get(fuente.nombre, ""))
                t.notas.insert(0, "👀 Es uno de tus productos: %s" % seguido["nombre"])
                notificador.enviar(t)
                almacen.registrar_aviso(clave, "comunidad_seguido", c.precio_cent, MODO, notificador.nombre,
                                        fecha=ahora)
                enviados += 1
                print("  seguido en comunidad: %s (%s)" % (c.titulo[:70], c.tienda))
            continue
        if c.fuente == "chollometro" and des["comunidad"].get("exigir_historico", True) and juez                 and c.precio_cent is not None and (not c.publicada or ahora - c.publicada <= max_ant)                 and not no_es_nuevo(c.titulo):
            juez.juzgar(c, ahora)
        tipo = evaluar_chollo(c, des, ahora, max_ant)
        clave = "%s:%s" % (fuente.nombre, c.id)
        if not tipo or _ya_avisado(almacen, clave, ["comunidad_error_precio", "comunidad_chollo",
                                                    "comunidad_interes"], c.precio_cent):
            continue
        # Un error de precio dura minutos: sale a cualquier hora. Los demas, en horario.
        if tipo != "error_precio" and not _en_horario(des, ahora):
            continue
        if almacen.avisos_desde(MODO, hoy) >= des["max_avisos_dia"] and tipo != "error_precio":
            continue
        t = tarjeta_chollo(c, tipo, ahora, atribuciones.get(fuente.nombre, ""))
        interes = interes_para(c.titulo, c.precio_cent, _ref_interes(c), des, catalogo=not c.es_oferta)
        if interes and tipo == "interes":
            t.nombre = "%s · %s" % (interes["nombre"], t.nombre) if len(t.nombre) < 90 else t.nombre
            t.categoria = interes["nombre"]
        elif interes:
            t.notas.insert(0, "🛍️ De tu lista: %s" % interes["nombre"])
        notificador.enviar(t)
        almacen.registrar_aviso(clave, "comunidad_%s" % tipo, c.precio_cent, MODO, notificador.nombre, fecha=ahora)
        enviados += 1
        print("  %s: %s (%s)" % (tipo, c.titulo[:70], c.tienda))
    return enviados


def tarjeta_chollo(c, tipo: str, ahora: datetime, atribucion: str) -> Tarjeta:
    real = c.ref_hist_cent and c.ref_hist_cent > (c.precio_cent or 0)
    t = Tarjeta(tipo=tipo, nombre=c.titulo, url=c.url, comprobado=ahora, precio_cent=c.precio_cent,
                referencia_cent=c.ref_hist_cent if real else c.precio_antes_cent,
                referencia_etiqueta="habitual" if real else "antes",
                categoria=c.categoria, imagen=c.imagen, tienda=c.tienda or None,
                boton="🔥 Ver el chollo" if c.fuente == "chollometro" else
                ("🛒 Ver en %s" % c.tienda if c.fuente == "pccomtracker" else "📈 Ver en reacofertas"))
    minutos = int((ahora - c.publicada).total_seconds() // 60) if c.publicada else None
    if c.minimo_cent and c.precio_cent is not None:
        f = " el %s" % c.minimo_fecha.strftime("%d/%m") if c.minimo_fecha else ""
        if c.precio_cent < c.minimo_cent:
            t.veredicto = "🏆 Nuevo mínimo %s (antes %s%s)" % (c.minimo_texto, eur(c.minimo_cent), f)
        elif c.precio_cent == c.minimo_cent:
            t.veredicto = "🏆 Iguala su mínimo %s%s" % (c.minimo_texto, f)
        else:
            t.veredicto = "📊 Su mínimo %s: %s%s" % (c.minimo_texto, eur(c.minimo_cent), f)
    if real:
        t.notas.append("ℹ️ Referencia real: %s" % c.ref_texto)
    elif c.precio_antes_cent:
        t.notas.append("ℹ️ Precio anterior %s, sin historial suficiente" % (
            "según la publicación" if c.fuente == "chollometro" else "según %s" % c.fuente))
    if tipo == "error_precio":
        t.notas.append("⚠️ Sin comprobar en la tienda: puede ser un error y durar poco")
    if c.fuente == "chollometro":
        t.fuente = "precio según Chollometro" + (" · publicado" + _hace(minutos) if minutos is not None else "")
        if c.pagina:
            t.enlaces = [("📈 Historial", c.pagina)]
    elif c.fuente == "pccomtracker":
        t.fuente = "precio comprobado por pccomtracker" + (_hace(minutos) if minutos is not None else "")
    else:
        t.fuente = "reacofertas (actualiza a diario)"
    return t


def _hace(minutos: int) -> str:
    if minutos < 90:
        return " hace %d min" % max(minutos, 0)
    return " hace %d h" % round(minutos / 60) if minutos < 48 * 60 else ""


def _resumen(h: Hallazgo, motivo: str) -> dict:
    return {"titulo": h.cand.titulo, "nivel": h.nivel, "precio_cent": h.cand.precio_cent,
            "referencia_cent": h.referencia_cent, "minimo_cent": h.minimo_cent, "fuente": h.cand.fuente,
            "motivo": motivo}


TIPO_TARJETA = {"error": "error_precio", "minimo_externo": "minimo_365", "minimo_local": "minimo_registrado",
                "oferta": "chollo", "interes": "interes"}


def tarjeta(h: Hallazgo, ahora: datetime, atribucion: str) -> Tarjeta:
    c = h.cand
    t = Tarjeta(tipo=TIPO_TARJETA[h.nivel], nombre=c.titulo, url=c.url, comprobado=ahora,
                precio_cent=c.precio_cent, referencia_cent=h.referencia_cent,
                referencia_etiqueta=h.referencia_etiqueta, minimo_anterior_cent=h.minimo_cent,
                minimo_periodo=h.periodo, categoria=h.interes if h.nivel == "interes" else c.categoria,
                imagen=c.imagen, asin=c.asin, tienda="Amazon")
    if h.nivel == "minimo_externo":
        t.veredicto = "🏆 Por debajo de su mínimo de 365 días (%s)" % eur(h.minimo_cent)
    elif h.nivel == "minimo_local":
        t.veredicto = "🏆 Lo más barato que he visto (%s, %s)" % (eur(h.minimo_cent), h.periodo)
    elif h.ref_real:
        t.veredicto = "📊 Referencia real: lo que he visto de este producto en los últimos 90 días"
    elif h.referencia_cent:
        t.veredicto = "ℹ️ Frente al precio de referencia de Amazon, no a un histórico"
    if h.verificado and c.vendedor and c.vendedor not in ("Amazon", "Amazon.es"):
        t.vendedor = c.vendedor
    if not h.verificado:
        t.disponibilidad = "ℹ️ Sin comprobar en Amazon"
    if h.interes and h.nivel == "interes":
        t.nombre = "%s · %s" % (h.interes, t.nombre) if len(t.nombre) < 90 else t.nombre
    elif h.interes:
        t.notas.append("🛍️ De tu lista: %s" % h.interes)
    if h.nivel == "error" and h.verificado:
        t.tipo = "error_comprobado"
        t.notas.append("✅ Precio y stock comprobados en Amazon a las %s" % hora_madrid(ahora).strftime("%H:%M"))
    elif h.nivel == "error":
        t.notas.append("⚠️ Sin comprobar en Amazon: puede ser un error y durar poco")
    if c.valoracion:
        t.notas.append("⭐ %s (%s opiniones)" % (str(c.valoracion).replace(".", ","),
                                                  "{:,}".format(c.num_valoraciones or 0).replace(",", ".")))
    t.fuente = "CholloMazon (CC BY 4.0)"
    t.condiciones = ["envío aparte"]
    if c.cupon:
        t.condiciones.append("cupón: %s" % c.cupon)
    return t
