"""Modo 2: descubrir ofertas por categorias, sin intervencion manual.

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

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import historial_amazon
from .almacen import Almacen
from .fuentes import Candidato, FuenteNoDisponible, SinNovedades
from .modelo import NUEVO, Observacion, eur, fecha_corta, hora_madrid, iso, desde_iso
from .tarjeta import Tarjeta

MODO = "descubrimiento"
NIVELES = ["minimo_externo", "minimo_local", "oferta"]


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
    if des.get("solo_nuevo", True) and (c.condicion != NUEVO or not c.disponible):
        return "no es nuevo"
    if not _categoria_ok(c.categoria, des):
        return "categoria"
    if not des["precio_min"] * 100 <= c.precio_cent <= des["precio_max"] * 100:
        return "precio fuera de rango"
    if c.comprobada and ahora - c.comprobada > max_antiguedad:
        return "dato antiguo"
    if des["marcas"] and (c.marca or "").lower() not in [m.lower() for m in des["marcas"]]:
        return "marca"
    if des["palabras_clave"] and not any(k.lower() in titulo for k in des["palabras_clave"]):
        return "palabras clave"
    if any(k.lower() in titulo for k in des["excluir_palabras"]):
        return "palabra excluida"
    return None


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

    # 3. Oferta interesante: frente al precio de referencia de Amazon.
    oi = des["oferta_interesante"]
    ref = c.precio_referencia_cent
    if avisar["ofertas_interesantes"] and ref and ref > precio:
        ahorro = ref - precio
        if ahorro >= oi["ahorro_min_eur"] * 100 and ahorro * 100 >= oi["descuento_min_pct"] * ref:
            return Hallazgo(c, "oferta", ahorro, ref, "Precio de referencia de Amazon")
    return None


def _verificar(h: Hallazgo, des: dict, leer) -> str | None:
    """Comprueba el finalista en la ficha de Amazon. None si vale; si no, el motivo."""
    try:
        obs = leer(h.cand.asin)
    except Exception as exc:  # noqa: BLE001
        return "no se pudo comprobar en Amazon (%s)" % type(exc).__name__
    if not obs.disponible or obs.precio_cent is None:
        return "sin unidades nuevas en Amazon"
    if obs.precio_cent > h.cand.precio_cent + 50:
        return "en Amazon ya cuesta %s" % eur(obs.precio_cent)
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


def ejecutar(cfg: dict, almacen: Almacen, notificador, fuentes: list, ahora: datetime,
             leer=None, dormir=time.sleep, seguidos: set[str] | None = None) -> int:
    """fuentes: objetos con nombre, atribucion y candidatos(). leer: lector de la
    ficha de Amazon para verificar (None = sin verificar)."""
    des = cfg["descubrimiento"]
    if not des["activo"]:
        return 0
    seguidos = seguidos or set()

    candidatos, atribuciones = [], {}
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
        for c in lote:
            _marcar_seguidos(almacen, seguidos, c)
            candidatos.append((c, max_ant))
        print("Descubrimiento: %s trae %d ofertas" % (fuente.nombre, len(lote)))

    if not candidatos:
        return 0
    hallazgos, vistos = [], set()
    for c, max_ant in candidatos:
        motivo = pasa_filtros(c, des, max_ant, ahora)
        h = None if motivo or c.asin in vistos else clasificar(c, des, almacen, ahora)
        vistos.add(c.asin)
        _guardar_observacion(almacen, c, ahora)
        if h:
            hallazgos.append(h)

    if not _en_horario(des, ahora):
        print("Descubrimiento: %d hallazgos, fuera de horario de avisos" % len(hallazgos))
        return 0
    hoy = hora_madrid(ahora).replace(hour=0, minute=0, second=0, microsecond=0)
    cupo = min(des["max_avisos_ejecucion"], des["max_avisos_dia"] - almacen.avisos_desde(MODO, hoy))
    verificaciones = des["max_verificaciones_dia"] - int(almacen.meta("verificaciones_%s" % hoy.date()) or 0)

    hallazgos.sort(key=lambda h: h.puntuacion, reverse=True)
    enviados, primera_lectura = 0, True
    for h in hallazgos:
        if almacen.aviso_enviado(h.cand.asin, _evento(h), h.cand.precio_cent):
            continue
        if enviados >= max(cupo, 0):
            almacen.registrar_candidato(h.cand.asin, _resumen(h, "fuera de cupo"), notificado=False)
            continue
        if des["verificar_en_amazon"] and leer:
            if verificaciones <= 0:
                almacen.registrar_candidato(h.cand.asin, _resumen(h, "sin verificaciones hoy"), notificado=False)
                continue
            if not primera_lectura:
                dormir(cfg["seguimiento"]["pausa_entre_amazon_seg"])
            primera_lectura = False
            verificaciones -= 1
            almacen.poner_meta("verificaciones_%s" % hoy.date(),
                               str(des["max_verificaciones_dia"] - verificaciones))
            motivo = _verificar(h, des, leer)
            if motivo:
                almacen.registrar_candidato(h.cand.asin, _resumen(h, motivo), notificado=False)
                print("  descartado %s: %s" % (h.cand.asin, motivo))
                continue
        notificador.enviar(tarjeta(h, ahora, atribuciones.get(h.cand.fuente, "")))
        almacen.registrar_aviso(h.cand.asin, _evento(h), h.cand.precio_cent, MODO, notificador.nombre, fecha=ahora)
        almacen.registrar_candidato(h.cand.asin, _resumen(h, "notificado"), notificado=True)
        enviados += 1
    print("Descubrimiento: %d hallazgos, %d avisos" % (len(hallazgos), enviados))
    return enviados


def _resumen(h: Hallazgo, motivo: str) -> dict:
    return {"titulo": h.cand.titulo, "nivel": h.nivel, "precio_cent": h.cand.precio_cent,
            "referencia_cent": h.referencia_cent, "minimo_cent": h.minimo_cent, "fuente": h.cand.fuente,
            "motivo": motivo}


TIPO_TARJETA = {"minimo_externo": "minimo_365", "minimo_local": "minimo_registrado", "oferta": "oferta"}


def tarjeta(h: Hallazgo, ahora: datetime, atribucion: str) -> Tarjeta:
    c = h.cand
    t = Tarjeta(tipo=TIPO_TARJETA[h.nivel], nombre=c.titulo, url=c.url, comprobado=ahora,
                precio_cent=c.precio_cent, referencia_cent=h.referencia_cent,
                referencia_etiqueta=h.referencia_etiqueta, minimo_anterior_cent=h.minimo_cent,
                minimo_periodo=h.periodo, categoria=c.categoria, imagen=c.imagen, asin=c.asin)
    if h.verificado:
        vendido = ", vendido por %s" % c.vendedor if c.vendedor else ""
        t.disponibilidad = "✅ En stock · Nuevo%s (comprobado en Amazon)" % vendido
    else:
        t.disponibilidad = "ℹ️ Sin comprobar en Amazon (según %s)" % c.fuente
    if c.valoracion:
        t.notas.append("⭐ %s/5 (%s valoraciones)" % (str(c.valoracion).replace(".", ","),
                                                       "{:,}".format(c.num_valoraciones or 0).replace(",", ".")))
    if h.nivel == "oferta":
        t.notas.append("ℹ️ Descuento frente al precio de referencia que muestra Amazon, no frente a un histórico")
    t.fuente = atribucion
    t.condiciones = ["Envío no incluido"]
    if c.cupon:
        t.condiciones.append("Cupón: %s" % c.cupon)
    return t
