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
    enviados = _procesar_comunidad(comunidad, cfg, almacen, notificador, ahora, hoy, atribuciones)
    if not candidatos:
        return enviados
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
        return enviados
    cupo = min(des["max_avisos_ejecucion"], des["max_avisos_dia"] - almacen.avisos_desde(MODO, hoy))
    verificaciones = des["max_verificaciones_dia"] - int(almacen.meta("verificaciones_%s" % hoy.date()) or 0)

    hallazgos.sort(key=lambda h: h.puntuacion, reverse=True)
    primera_lectura, enviados_antes = True, enviados
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
    print("Descubrimiento: %d hallazgos, %d avisos" % (len(hallazgos), enviados - enviados_antes))
    return enviados


# ------------------------------------------------------- chollos de la comunidad
def evaluar_chollo(c, des: dict, ahora: datetime, max_ant: timedelta) -> str | None:
    """'error_precio', 'chollo' o None. Un error de precio es lo que la comunidad
    marca como tal; un chollo, un descuento >= descuento_min_pct frente al precio
    anterior que da la propia publicacion."""
    com = des["comunidad"]
    texto = c.texto.lower()
    if c.publicada and ahora - c.publicada > max_ant:
        return None
    if any(k.lower() in texto for k in des["excluir_palabras"]):
        return None
    if des["palabras_clave"] and not any(k.lower() in texto for k in des["palabras_clave"]):
        return None
    if com["tiendas"] and c.tienda.lower() not in [t.lower() for t in com["tiendas"]]:
        return None
    # Solo los temas que te interesan: para ver de todo ya esta Chollometro.
    if com["categorias"] and (c.categoria or "").lower() not in [k.lower() for k in com["categorias"]]:
        return None
    if c.precio_cent is not None and c.precio_cent > des["precio_max"] * 100:
        return None
    # Lo barato no interesa aunque sea un buen descuento: unos peines de 3 EUR o
    # un polo de 15 EUR rebajado no son un chollo que merezca un aviso.
    valor_ok = c.precio_antes_cent is not None and c.precio_antes_cent >= com["valor_min_eur"] * 100
    ahorro = (c.precio_antes_cent - c.precio_cent) if valor_ok and c.precio_cent is not None else 0
    # Error de precio: solo si la comunidad lo pone en el TITULO (en la
    # descripcion sale en frases como "me parece un preciazo").
    titulo = c.titulo.lower()
    if any(k.lower() in titulo for k in com["palabras_error"]):
        if c.precio_cent is None:
            return None
        if valor_ok and ahorro >= com["ahorro_min_eur"] * 100:
            return "error_precio"
        if not c.precio_antes_cent and c.precio_cent >= com["precio_min_error_eur"] * 100:
            return "error_precio"  # sin precio anterior: al menos que no sea calderilla
        return None
    if valor_ok and ahorro * 100 >= com["descuento_min_pct"] * c.precio_antes_cent \
            and ahorro >= com["ahorro_min_eur"] * 100:
        return "chollo"
    return None


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


def _procesar_comunidad(lote, cfg, almacen: Almacen, notificador, ahora, hoy, atribuciones) -> int:
    des = cfg["descubrimiento"]
    enviados = 0
    for fuente, c, max_ant in lote:
        seguido = producto_en_chollo(c, cfg.get("productos", []))
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
        tipo = evaluar_chollo(c, des, ahora, max_ant)
        clave = "%s:%s" % (fuente.nombre, c.id)
        if not tipo or almacen.aviso_enviado(clave, "comunidad_%s" % tipo, c.precio_cent):
            continue
        # Un error de precio dura minutos: sale a cualquier hora. Los demas, en horario.
        if tipo != "error_precio" and not _en_horario(des, ahora):
            continue
        if almacen.avisos_desde(MODO, hoy) >= des["max_avisos_dia"] and tipo != "error_precio":
            continue
        notificador.enviar(tarjeta_chollo(c, tipo, ahora, atribuciones.get(fuente.nombre, "")))
        almacen.registrar_aviso(clave, "comunidad_%s" % tipo, c.precio_cent, MODO, notificador.nombre, fecha=ahora)
        enviados += 1
        print("  %s: %s (%s)" % (tipo, c.titulo[:70], c.tienda))
    return enviados


def tarjeta_chollo(c, tipo: str, ahora: datetime, atribucion: str) -> Tarjeta:
    t = Tarjeta(tipo=tipo, nombre=c.titulo, url=c.url, comprobado=ahora, precio_cent=c.precio_cent,
                referencia_cent=c.precio_antes_cent, referencia_etiqueta="Antes (según la publicación)",
                categoria=c.categoria, imagen=c.imagen, boton="🔥 Ver el chollo")
    minutos = int((ahora - c.publicada).total_seconds() // 60) if c.publicada else None
    hace = (" hace %d min" % minutos) if minutos is not None and minutos < 180 else ""
    t.disponibilidad = "🏬 %s · publicado%s" % (c.tienda or "Tienda sin indicar", hace)
    if tipo == "error_precio":
        t.notas.append("⚡ La comunidad lo marca como error de precio: suelen durar poco")
    t.fuente = atribucion
    return t


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
    if h.nivel == "oferta" and h.referencia_cent and h.ahorro_cent * 100 >= 70 * h.referencia_cent:
        t.tipo = "chollo"
    if h.nivel == "oferta":
        t.notas.append("ℹ️ Descuento frente al precio de referencia que muestra Amazon, no frente a un histórico")
    t.fuente = atribucion
    t.condiciones = ["Envío no incluido"]
    if c.cupon:
        t.condiciones.append("Cupón: %s" % c.cupon)
    return t
