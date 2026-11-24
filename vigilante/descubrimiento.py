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

from . import fuentes_historico, historia, historial_amazon, niveles
from .almacen import Almacen
from .fuentes import Candidato, FuenteNoDisponible, SinNovedades
from .modelo import NUEVO, Observacion, eur, fecha_corta, hora_madrid, iso, desde_iso
from .tarjeta import Tarjeta

MODO = "descubrimiento"
NIVELES = ["error", "chollazo", "minimo_externo", "minimo_local", "extraordinaria", "oportunidad", "interes"]
# Nivel -> tipo de tarjeta.
TIPO_NIVEL = {"error": "error_precio", "chollazo": "chollo", "extraordinaria": "extraordinaria"}
ETIQUETA_REF = {"min30": "mín. 30 días", "mediano": "mediano", "habitual": "habitual", "bot": "habitual",
                "anunciado": "PVPR"}
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
    veredicto: object = None    # niveles.Veredicto
    obs: object = None          # lectura de la ficha (verificacion)
    leida: bool = False         # se ha intentado leer la ficha en esta pasada

    @property
    def puntuacion(self) -> tuple:
        calidad = (self.cand.valoracion or 4) / 5
        anunciado = (self.cand.extra or {}).get("anunciado_pct") or 0
        return (-NIVELES.index(self.nivel), self.ahorro_cent * calidad, anunciado)


def tipo_precio(fuente: str) -> str:
    return "oferta_%s" % fuente


def intervalo(nombre: str, ajustes: dict, des: dict, ahora: datetime) -> float:
    """Minutos hasta la siguiente lectura de una fuente. En las ventanas
    rapidas (p. ej. de 06:55 a 10:30, cuando salen las ofertas flash) se mira
    mas a menudo, sin bajar nunca del minimo de la fuente."""
    from .config import MIN_INTERVALO_FUENTE
    t = hora_madrid(ahora).strftime("%H:%M")
    factor = min([v["factor"] for v in des.get("ventanas_rapidas", []) if v["desde"] <= t < v["hasta"]] or [1])
    return max(ajustes["intervalo_min"] * factor, MIN_INTERVALO_FUENTE.get(nombre, 1))


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
    """Filtros de los minimos y los intereses (no de los niveles excepcionales,
    que valen en cualquier categoria). None si pasa; si no, el motivo."""
    titulo = c.titulo.lower()
    base = filtro_base(c, des, max_antiguedad, ahora)
    if base:
        return base
    if c.precio_cent is None:
        return "sin precio"
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
    if c.precio_cent is not None and c.precio_cent < des["precio_min"] * 100:
        return "precio fuera de rango"
    return None


def clasificar_todo(c: Candidato, des: dict, almacen: Almacen, ahora: datetime,
                    max_antiguedad: timedelta) -> Hallazgo | None:
    """Antes de leer la ficha: minimos e intereses (con su precio) y los
    candidatos a oferta excepcional ("oportunidad"), que se deciden despues
    con la referencia real de la ficha. El descuento anunciado solo sirve
    para elegir que fichas merece la pena leer."""
    if filtro_base(c, des, max_antiguedad, ahora):
        return None
    ref = c.precio_referencia_cent
    anunciado = (c.extra or {}).get("anunciado_pct")
    if anunciado is None and c.precio_cent is not None:
        anunciado = descuento(c.precio_cent, ref)[1]
    bot = historia.referencia_bot(almacen, historia.clave("Amazon", asin=c.asin), ahora) \
        if c.precio_cent is not None else None
    caida = descuento(c.precio_cent, bot)[1] if bot else 0
    interes = interes_para(c.titulo, c.precio_cent, ref, des) if c.precio_cent is not None else None
    h = None
    if c.precio_cent is not None and pasa_filtros(c, des, max_antiguedad, ahora) is None:
        h = clasificar(c, des, almacen, ahora)
    minimo_ahorro = min(des["niveles"]["chollazo"]["ahorro_min_eur"],
                        *[r["ahorro_min_eur"] for r in des["niveles"]["extraordinaria"]["reglas"]]) * 100
    ahorro = max(descuento(c.precio_cent, ref)[0], descuento(c.precio_cent, bot)[0])         if c.precio_cent is not None else None
    if h is None and max(anunciado or 0, caida) >= des.get("prefiltro_anunciado_pct", 40)             and (ahorro is None or ahorro >= minimo_ahorro):
        # Candidata a oferta excepcional: se decide al leer la ficha. Se leen
        # primero las de mas ahorro anunciado (la calderilla no llega).
        h = Hallazgo(c, "oportunidad", ahorro or 0, ref, "ref. Amazon")
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
    """Minimos de la serie de la fuente (365 d importado o lo visto por el bot).
    Los chollazos y errores los decide niveles.evaluar tras leer la ficha."""
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
    return None


def _con_valoraciones(h: Hallazgo, des: dict) -> bool:
    grupo = {"chollazo": "chollo", "extraordinaria": "chollo", "oportunidad": "chollo", "interes": "interes",
             "minimo_local": "minimo", "minimo_externo": "minimo"}
    return grupo.get(h.nivel) in des.get("valoraciones_en", [])


def _verificar(h: Hallazgo, des: dict, leer) -> str | None:
    """Lee la ficha de Amazon del finalista: precio, unidades nuevas y sus
    referencias (minimo de 30 dias, mediano, PVPR). Las valoraciones solo se
    exigen en los niveles de valoraciones_en, nunca a un error de precio.
    None si vale; si no, el motivo."""
    try:
        obs = leer(h.cand.asin)
    except Exception as exc:  # noqa: BLE001
        return "no se pudo comprobar en Amazon (%s)" % type(exc).__name__
    h.obs = obs
    if not obs.disponible or obs.precio_cent is None:
        return "sin unidades nuevas en Amazon"
    if h.cand.precio_cent is not None and obs.precio_cent > h.cand.precio_cent + 50 and h.nivel != "oportunidad":
        return "en Amazon ya cuesta %s" % eur(obs.precio_cent)
    if _con_valoraciones(h, des) and not _es_error_claro(h, obs, des):
        if obs.valoracion is None or obs.valoracion < des["valoracion_min"]:
            return "valoracion %s" % obs.valoracion
        if (obs.num_valoraciones or 0) < des["valoraciones_min"]:
            return "pocas valoraciones (%s)" % obs.num_valoraciones
    bajada = (h.cand.precio_cent - obs.precio_cent) if h.cand.precio_cent is not None else 0
    h.cand.precio_cent = obs.precio_cent
    h.ahorro_cent += max(bajada, 0)
    h.cand.valoracion, h.cand.num_valoraciones = obs.valoracion, obs.num_valoraciones
    h.cand.imagen, h.cand.vendedor = obs.imagen or h.cand.imagen, obs.vendedor
    if obs.titulo:
        h.cand.titulo = obs.titulo
    h.verificado = True
    return None


def _es_error_claro(h: Hallazgo, obs, des: dict) -> bool:
    """Un posible error de precio no se descarta por valoraciones."""
    v = niveles.evaluar(_evidencia(h, obs, None), des["niveles"], h.cand.fuente)
    return bool(v and v.nivel == "error")


def _evidencia(h: Hallazgo, obs, bot: int | None) -> niveles.Evidencia:
    c = h.cand
    precio = obs.precio_cent if obs is not None and obs.precio_cent is not None else c.precio_cent
    anunciado = (obs.pvpr_cent if obs is not None else None) or c.precio_referencia_cent
    return niveles.Evidencia(precio=precio, min30=obs.min30_cent if obs is not None else None,
                             mediano=obs.mediano_cent if obs is not None else None, bot=bot,
                             anunciado=anunciado,
                             anunciado_tipo="PVPR" if obs is not None and obs.pvpr_cent else "ref. Amazon",
                             textos={"min30": "su mínimo de 30 días en Amazon",
                                     "mediano": "su precio mediano en Amazon",
                                     "bot": "lo que he visto en 30 días"})


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
        almacen.poner_meta("proxima_%s" % fuente.nombre,
                           iso(ahora + timedelta(minutes=intervalo(fuente.nombre, ajustes, des, ahora))))
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
        if c.precio_cent is not None:
            _guardar_observacion(almacen, c, ahora)
            historia.registrar(almacen, historia.clave("Amazon", asin=c.asin), c.fuente, "Amazon", c.precio_cent,
                               ahora, anunciado=c.precio_referencia_cent, asin=c.asin, titulo=c.titulo, disponible=1)
        if c.asin in vistos or c.asin in seguidos:
            continue  # un producto que sigues lo avisa el seguimiento, no el descubrimiento
        vistos.add(c.asin)
        h = clasificar_todo(c, des, almacen, ahora, max_ant)
        if h:
            hallazgos.append(h)

    en_horario = _en_horario(des, ahora)
    if not en_horario:
        # Fuera de horario solo salen errores y chollazos (duran poco): se
        # siguen comprobando las oportunidades, que pueden serlo.
        hallazgos = [h for h in hallazgos if h.nivel == "oportunidad"]
    cupo = min(des["max_avisos_ejecucion"], des["max_avisos_dia"] - almacen.avisos_desde(MODO, hoy))
    verificaciones = des["max_verificaciones_dia"] - int(almacen.meta("verificaciones_%s" % hoy.date()) or 0)

    hallazgos.sort(key=lambda h: h.puntuacion, reverse=True)
    primera_lectura, enviados_antes = True, enviados
    eventos = ["descubrimiento_%s" % n for n in NIVELES]
    lecturas_ejecucion = des.get("max_verificaciones_ejecucion", 6)
    realerta = des["niveles"]["realerta_bajada_pct"]
    for h in hallazgos:
        k = historia.clave("Amazon", asin=h.cand.asin)
        if h.cand.precio_cent is not None and (_ya_avisado(almacen, h.cand.asin, eventos, h.cand.precio_cent)
                                               or historia.ya_avisada(almacen, k, h.cand.precio_cent, realerta)):
            continue
        if h.nivel == "oportunidad":
            hecho = _oportunidad(h, des, almacen, leer, ahora, dormir, cfg, primera_lectura, lecturas_ejecucion,
                                 hoy)
            if h.leida:  # cuenta toda ficha leida, valga o no
                primera_lectura = False
                lecturas_ejecucion -= 1
            if hecho is None:
                continue
            if not en_horario and h.nivel == "extraordinaria":
                almacen.registrar_candidato(h.cand.asin, _resumen(h, "fuera de horario"), notificado=False)
                continue
            # Errores y chollazos no gastan el cupo diario; las extraordinarias si.
            tope = des["max_avisos_ejecucion"] if h.nivel in ("error", "chollazo") else cupo
            if enviados - enviados_antes >= max(tope, 0):
                almacen.registrar_candidato(h.cand.asin, _resumen(h, "fuera de cupo"), notificado=False)
                continue
            if historia.ya_avisada(almacen, k, h.cand.precio_cent, realerta):
                continue
            notificador.enviar(tarjeta(h, ahora, atribuciones.get(h.cand.fuente, "")))
            historia.anotar_aviso(almacen, k, h.cand.precio_cent, MODO, notificador.nombre, ahora)
            almacen.registrar_aviso(h.cand.asin, _evento(h), h.cand.precio_cent, MODO, notificador.nombre, fecha=ahora)
            almacen.registrar_candidato(h.cand.asin, _resumen(h, "notificado"), notificado=True)
            enviados += 1
            print("  %s: %s (%s)" % (h.nivel, h.cand.titulo[:70], eur(h.cand.precio_cent)))
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
        historia.anotar_aviso(almacen, k, h.cand.precio_cent, MODO, notificador.nombre, ahora)
        almacen.registrar_candidato(h.cand.asin, _resumen(h, "notificado"), notificado=True)
        enviados += 1
    print("Descubrimiento: %d hallazgos, %d avisos" % (len(hallazgos), enviados - enviados_antes))
    return enviados


def _oportunidad(h: Hallazgo, des: dict, almacen: Almacen, leer, ahora, dormir, cfg, primera_lectura: bool,
                 lecturas_ejecucion: int, hoy) -> bool | None:
    """Lee la ficha del candidato (si se puede) y decide su nivel con la
    referencia real. True si hay que avisar; None si no."""
    k = historia.clave("Amazon", asin=h.cand.asin)
    clave_lectura = "leida:%s:%s" % (h.cand.asin, (h.cand.extra or {}).get("deal") or h.cand.precio_cent)
    leida = almacen.meta(clave_lectura)
    if leida and ahora - desde_iso(leida) < timedelta(hours=6):
        return None  # esta misma oferta se comprobo hace poco y no era para tanto
    hechas = int(almacen.meta("verificaciones_%s" % hoy.date()) or 0)
    if leer and des["verificar_en_amazon"] and lecturas_ejecucion > 0 and hechas < des["max_verificaciones_dia"]:
        if not primera_lectura:
            dormir(cfg["seguimiento"]["pausa_entre_amazon_seg"])
        almacen.poner_meta("verificaciones_%s" % hoy.date(), str(hechas + 1))
        h.leida = True
        motivo = _verificar(h, des, leer)
        if h.obs is not None and h.obs.precio_cent is not None:
            o = h.obs
            historia.registrar(almacen, k, "amazon_ficha", "Amazon", o.precio_cent, ahora, min30=o.min30_cent,
                               anunciado=o.pvpr_cent, asin=h.cand.asin, titulo=o.titulo, disponible=int(o.disponible))
        if motivo and not motivo.startswith("no se pudo comprobar"):
            almacen.poner_meta(clave_lectura, iso(ahora))
            almacen.registrar_candidato(h.cand.asin, _resumen(h, motivo), notificado=False)
            print("  descartado %s: %s" % (h.cand.asin, motivo))
            return None
    if h.cand.precio_cent is None:
        return None  # sin leer la ficha no se sabe el precio (Amazon /deals)
    bot = historia.referencia_bot(almacen, k, ahora)
    v = niveles.evaluar(_evidencia(h, h.obs, bot), des["niveles"], h.cand.fuente)
    if not h.verificado and not (v and v.nivel == "error"):
        # Sin leer la ficha solo sale un posible error de precio (dura poco).
        # El resto espera a la siguiente pasada, sin darse por descartado.
        return None
    if not v:
        almacen.poner_meta(clave_lectura, iso(ahora))
        almacen.registrar_candidato(h.cand.asin, _resumen(h, "descuento real insuficiente"), notificado=False)
        print("  sin chollo real: %s (%s)" % (h.cand.titulo[:60], eur(h.cand.precio_cent)))
        return None
    h.veredicto, h.nivel = v, v.nivel
    h.referencia_cent, h.ahorro_cent, h.ref_real = v.referencia, v.ahorro, not v.provisional
    return True


# ------------------------------------------------------- chollos de la comunidad
def evaluar_chollo(c, des: dict, ahora: datetime, max_ant: timedelta) -> str | None:
    """'error_precio', 'chollo' (chollazo), 'extraordinaria', 'interes' o None.

    Los tres niveles excepcionales los decide niveles.evaluar con la referencia
    real (minimo de 30 dias, habitual o lo visto por el bot), en CUALQUIER
    categoria; el "antes" de una publicacion solo cuenta para una alerta
    provisional y solo en las fuentes que lo permiten. Ademas, un error de
    precio que la comunidad marca en el TITULO (con historia real si es de
    Chollometro). Lo barato no interesa: unos peines de 3 EUR o un polo de 15
    EUR rebajado no merecen un aviso."""
    com = des["comunidad"]
    texto, titulo = c.texto.lower(), c.titulo.lower()
    if c.precio_cent is None or (c.publicada and ahora - c.publicada > max_ant):
        return None
    # Chollometro solo detecta: sin la historia real del producto no avisa.
    if c.fuente == "chollometro" and com.get("exigir_historico", True) and not (c.ref_hist_cent or c.min30_cent):
        return None
    if any(k.lower() in texto for k in des["excluir_palabras"]):
        return None
    if des.get("solo_nuevo", True) and no_es_nuevo(c.titulo):
        return None
    if com["tiendas"] and c.tienda.lower() not in [t.lower() for t in com["tiendas"]]:
        return None
    if fuentes_historico.oferta_vieja(c.precio_cent, c.minimo_cent, c.minimo_fecha, ahora) and not c.min30_cent:
        c.min30_cent = c.minimo_cent  # su minimo reciente: frente a el no hay descuento
    ev = niveles.Evidencia(precio=c.precio_cent, min30=c.min30_cent, habitual=c.ref_hist_cent, bot=c.ref_bot_cent,
                           anunciado=c.precio_antes_cent,
                           anunciado_tipo="antes" if c.fuente == "chollometro" else "precio anterior",
                           textos={"min30": "su mínimo de 30 días", "habitual": c.ref_texto or "precio habitual",
                                   "bot": "lo que he visto en 30 días"})
    v = niveles.evaluar(ev, des["niveles"], c.fuente)
    if v is None and any(k.lower() in titulo for k in com["palabras_error"]):
        ref = c.ref_hist_cent or c.min30_cent or c.precio_antes_cent
        ahorro, _ = descuento(c.precio_cent, ref)
        err = des["niveles"]["error"]
        if (ref and ahorro >= err["ahorro_min_eur"] * 100 and ref >= err["valor_min_eur"] * 100) or \
                (not ref and c.precio_cent >= com["precio_min_error_eur"] * 100):
            v = niveles.Veredicto("error", descuento(c.precio_cent, ref)[1], ahorro, ref or 0,
                                  "comunidad", "lo marca la comunidad", provisional=not (c.ref_hist_cent or c.min30_cent),
                                  pendiente=["confirmar el precio en la tienda"])
    if v is not None:
        c.veredicto = v
        return TIPO_NIVEL[v.nivel]
    if interes_para(c.titulo, c.precio_cent, _ref_interes(c), des, catalogo=not c.es_oferta)             and not (not c.es_oferta and c.min30_cent and c.precio_cent >= c.min30_cent):
        return "interes"  # lo de un catalogo que lleva dias al mismo precio no es noticia
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
        k = historia.clave(c.tienda or fuente.nombre, titulo=c.titulo_ref or c.titulo)
        if c.precio_cent is not None:
            c.ref_bot_cent = historia.referencia_bot(almacen, k, ahora)
            historia.registrar(almacen, k, c.fuente, c.tienda, c.precio_cent, ahora, min30=c.min30_cent,
                               habitual=c.ref_hist_cent, anunciado=c.precio_antes_cent, titulo=c.titulo, disponible=1)
        tipo = evaluar_chollo(c, des, ahora, max_ant)
        clave = "%s:%s" % (fuente.nombre, c.id)
        if not tipo or _ya_avisado(almacen, clave, ["comunidad_error_precio", "comunidad_chollo",
                                                    "comunidad_extraordinaria", "comunidad_interes"], c.precio_cent):
            continue
        if tipo != "interes" and historia.ya_avisada(almacen, k, c.precio_cent, des["niveles"]["realerta_bajada_pct"]):
            continue  # la misma oferta ya llego por otra fuente
        # Errores y chollazos duran poco: salen a cualquier hora y sin gastar el
        # cupo diario. Lo demas, en horario.
        urgente = tipo in ("error_precio", "chollo")
        if not urgente and not _en_horario(des, ahora):
            continue
        if almacen.avisos_desde(MODO, hoy) >= des["max_avisos_dia"] and not urgente:
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
        if tipo != "interes":
            historia.anotar_aviso(almacen, k, c.precio_cent, MODO, notificador.nombre, ahora)
        enviados += 1
        print("  %s: %s (%s)" % (tipo, c.titulo[:70], c.tienda))
    return enviados


def tarjeta_chollo(c, tipo: str, ahora: datetime, atribucion: str) -> Tarjeta:
    v = c.veredicto
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
    if v is not None and v.referencia:
        t.referencia_cent, t.referencia_etiqueta = v.referencia, ETIQUETA_REF.get(v.referencia_tipo, "antes")
        _notas_veredicto(t, v, c.precio_antes_cent)
    elif real:
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


def _notas_veredicto(t: Tarjeta, v, anunciado: int | None) -> None:
    """Descuento real o provisional, y el anunciado si es distinto."""
    if v.provisional:
        t.notas.insert(0, "⏳ PROVISIONAL: −%d %% frente al %s. Pendiente: %s"
                       % (v.pct, v.referencia_texto, "; ".join(v.pendiente)))
    else:
        t.notas.insert(0, "✅ Descuento real: −%d %% (−%s) frente a %s"
                       % (v.pct, eur(v.ahorro), v.referencia_texto))
        if anunciado and v.anunciado_pct and abs(v.anunciado_pct - v.pct) >= 5:
            t.notas.append("🏷️ Anunciado: −%d %% sobre %s (descuento comercial)" % (v.anunciado_pct, eur(anunciado)))


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
    tipo = TIPO_NIVEL.get(h.nivel) or TIPO_TARJETA[h.nivel]
    t = Tarjeta(tipo=tipo, nombre=c.titulo, url=c.url, comprobado=ahora,
                precio_cent=c.precio_cent, referencia_cent=h.referencia_cent,
                referencia_etiqueta=h.referencia_etiqueta, minimo_anterior_cent=h.minimo_cent,
                minimo_periodo=h.periodo, categoria=h.interes if h.nivel == "interes" else c.categoria,
                imagen=c.imagen, asin=c.asin, tienda="Amazon")
    v = h.veredicto
    if v is not None:
        t.referencia_etiqueta = ETIQUETA_REF.get(v.referencia_tipo, "antes")
        anunciado = (h.obs.pvpr_cent if h.obs is not None else None) or c.precio_referencia_cent
        _notas_veredicto(t, v, anunciado)
    elif h.nivel == "minimo_externo":
        t.veredicto = "🏆 Por debajo de su mínimo de 365 días (%s)" % eur(h.minimo_cent)
    elif h.nivel == "minimo_local":
        t.veredicto = "🏆 Lo más barato que he visto (%s, %s)" % (eur(h.minimo_cent), h.periodo)
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
    t.fuente = "CholloMazon (CC BY 4.0)" if c.fuente == "chollomazon" else "Amazon.es, ficha leída por el bot"
    t.condiciones = ["envío aparte"]
    if c.cupon:
        t.condiciones.append("cupón: %s" % c.cupon)
    return t


