"""Modo 1: seguimiento de los productos de la configuracion.

Cada producto se lee cuando le toca segun el intervalo de su fuente. Una
lectura valida se guarda y pasa por las reglas; un fallo se guarda aparte y
el ultimo precio bueno no se toca. Tras un fallo se reintenta con esperas
crecientes (30 min, 1 h, 2 h...) sin superar nunca el intervalo normal.

Un aviso que no se puede enviar se queda pendiente y se reintenta en el
siguiente ciclo, en vez de perderse.
"""
from __future__ import annotations

import csv
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import check_price as cp

from . import amazon, config, historial_amazon, reglas
from .almacen import Almacen
from .modelo import Observacion, Producto, a_centimos, ahora_utc, desde_iso, eur, fecha_corta, iso
from .tarjeta import Tarjeta

VENTANA_DUPLICADOS = timedelta(hours=24)
ESPERA_TRAS_FALLO_MIN = 30


def _toca(p: Producto, est: dict, seg: dict, ahora: datetime) -> bool:
    ultimo = est.get("ultimo_intento")
    if not ultimo:
        return True
    if est.get("leer_ya"):
        # Lectura extra: el modo descubrimiento lo ha visto rebajado. Con un
        # minimo entre lecturas para no convertirlo en una consulta continua.
        return ahora - desde_iso(ultimo) >= timedelta(minutes=seg["lectura_extra_min_entre"])
    intervalo = seg["intervalo_min_por_fuente"][p.fuente]
    fallos = est.get("fallos", 0)
    if fallos:
        intervalo = min(intervalo, ESPERA_TRAS_FALLO_MIN * 2 ** (fallos - 1))
    # 2 min de margen: el bucle no cae siempre exactamente a la misma hora.
    return ahora - desde_iso(ultimo) >= timedelta(minutes=intervalo - 2)


def leer(p: Producto) -> Observacion:
    if p.fuente == "amazon_ficha":
        return amazon.leer(p.clave)
    precio, disponible, titulo = cp.fetch_shopify(
        {"url": p.url, "variant_id": p.extra["variant_id"], "nombre": p.nombre, "tipo": "shopify"})
    return Observacion(clave=p.clave, fecha=ahora_utc(), fuente="shopify", disponible=disponible,
                       precio_cent=a_centimos(precio) if disponible else None, titulo=titulo,
                       vendedor=p.extra.get("tienda"))


def _csv_compatible(ruta: Path, p: Producto, obs: Observacion) -> None:
    """Sigue anadiendo al CSV de siempre, con sus mismas columnas."""
    if obs.precio_cent is None:
        return
    nuevo = not ruta.exists()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with ruta.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["fecha_utc", "id", "producto", "precio_eur", "disponible",
                        "precio_sin_cupon_eur", "cupon"])
        w.writerow([iso(obs.fecha), p.clave, p.nombre, "%.2f" % (obs.precio_con_cupon_cent / 100),
                    obs.disponible, "%.2f" % (obs.precio_cent / 100) if obs.cupon_cent else "",
                    obs.cupon_texto or ""])


def tarjeta_para(p: Producto, obs: Observacion, res: reglas.Resultado, serie_previa,
                 ext) -> Tarjeta:
    motivo = res.principal
    t = Tarjeta(tipo=motivo.tipo, nombre=p.nombre if p.nombre != p.clave else (obs.titulo or p.nombre),
                url=p.url, comprobado=obs.fecha, precio_cent=obs.precio_cent, categoria=p.categoria,
                imagen=obs.imagen, asin=p.clave if p.fuente == "amazon_ficha" else None)
    if p.fuente != "amazon_ficha":
        t.boton = "🛒 Ver en la tienda"

    previo = serie_previa[-1][1] if serie_previa else None
    if res.media_90_cent and res.media_90_cent > obs.precio_cent:
        t.referencia_cent, t.referencia_etiqueta = res.media_90_cent, "Media 90 d"
    elif previo and previo > obs.precio_cent:
        t.referencia_cent, t.referencia_etiqueta = previo, "Antes"

    if motivo.tipo == "minimo_365":
        t.minimo_anterior_cent, t.minimo_periodo = motivo.datos["minimo_anterior"], "365 d"
    elif serie_previa:
        desde = motivo.datos.get("desde") or serie_previa[0][0]
        t.minimo_anterior_cent = motivo.datos.get("minimo_anterior") or min(x for _, x in serie_previa)
        t.minimo_periodo = "registrado desde %s" % fecha_corta(desde)[:10]

    vendido = ", vendido por %s" % obs.vendedor if obs.vendedor else ""
    t.disponibilidad = "✅ En stock · Nuevo%s" % vendido
    t.fuente = "Fuente: ficha de Amazon" if p.fuente == "amazon_ficha" else "Fuente: tienda oficial"
    if motivo.tipo == "minimo_365":
        t.fuente += " e historial de 365 d de Amazon (importado el %s, completado con mis lecturas)" \
            % fecha_corta(ext[0])[:10]
    if p.fuente == "amazon_ficha":
        t.condiciones.append("Envío no incluido")
    if obs.cupon_cent:
        t.condiciones.append("Cupón de %s: márcalo en la ficha y pagas %s"
                             % (obs.cupon_texto, eur(obs.precio_con_cupon_cent)))

    notas = {
        "reposicion": "📦 Vuelve a haber unidades nuevas",
        "bajada": "📉 Baja desde %s" % eur(previo) if previo else "",
        "objetivo": "🎯 Por debajo de tu objetivo de %s" % eur(p.precio_objetivo_cent or 0),
        "minimo_registrado": "🏆 También es el mínimo que he registrado",
        "minimo_igualado": "🔁 Iguala su mínimo registrado",
    }
    t.notas = [notas[m.tipo] for m in res.motivos[1:] if notas.get(m.tipo)]
    return t


def _enviar_pendiente(clave: str, est: dict, almacen: Almacen, notificador) -> None:
    pendiente = est.get("aviso_pendiente")
    if not pendiente:
        return
    datos = dict(pendiente["tarjeta"], comprobado=desde_iso(pendiente["tarjeta"]["comprobado"]))
    try:
        notificador.enviar(Tarjeta(**datos))
    except Exception as exc:  # noqa: BLE001
        print("  [%s] el aviso pendiente sigue sin poder enviarse: %s" % (clave, exc))
        return
    almacen.registrar_aviso(clave, datos["tipo"], datos["precio_cent"], "seguimiento", notificador.nombre)
    est.pop("aviso_pendiente")
    print("  [%s] enviado el aviso que estaba pendiente" % clave)


def ciclo(cfg: dict, almacen: Almacen, notificador, ahora: datetime | None = None, lector=leer,
          dormir=time.sleep) -> int:
    """Una pasada por los productos a los que les toca. Devuelve avisos enviados."""
    seg = cfg["seguimiento"]
    ruta_csv = Path(os.environ.get("HISTORY_FILE", config.RAIZ / "data" / "historico_precios.csv"))
    enviados, primero_amazon = 0, True
    for p in config.productos(cfg):
        ahora_p = ahora or ahora_utc()
        est = almacen.estado(p.clave)
        _enviar_pendiente(p.clave, est, almacen, notificador)
        if not _toca(p, est, seg, ahora_p):
            almacen.guardar_estado(p.clave, est)
            continue
        if p.fuente == "amazon_ficha":
            if not primero_amazon:
                dormir(seg["pausa_entre_amazon_seg"])
            primero_amazon = False

        est["ultimo_intento"] = iso(ahora_p)
        est.pop("leer_ya", None)
        try:
            obs = lector(p)
        except Exception as exc:  # noqa: BLE001
            est["fallos"] = est.get("fallos", 0) + 1
            almacen.registrar_fallo(p.clave, p.fuente, "%s: %s" % (type(exc).__name__, exc), ahora_p)
            print("[%s] fallo %d seguido: %s: %s" % (p.nombre, est["fallos"], type(exc).__name__, exc))
            if seg["avisos_tecnicos"] and est["fallos"] >= seg["fallos_antes_de_aviso_tecnico"] \
                    and not est.get("aviso_tecnico_enviado"):
                try:
                    notificador.enviar(Tarjeta(tipo="aviso_tecnico", nombre=p.nombre, url=p.url,
                                               comprobado=ahora_p, notas=[str(exc)[:300]]))
                    est["aviso_tecnico_enviado"] = True
                except Exception as exc2:  # noqa: BLE001
                    print("  no se pudo enviar el aviso tecnico: %s" % exc2)
            almacen.guardar_estado(p.clave, est)
            continue

        obs.fecha = ahora or obs.fecha
        serie_previa = almacen.serie(p.clave, hasta=obs.fecha)
        ext = almacen.historial_externo(p.clave, historial_amazon.FUENTE)
        lecturas = []
        if ext:
            lecturas = [desde_iso(r["fecha"]) for r in almacen.db.execute(
                "SELECT fecha FROM observaciones WHERE clave=? AND fecha>=?", (p.clave, iso(ext[0])))]
        res = reglas.evaluar(obs, p, serie_previa, est, seg["reglas"], obs.fecha, ext, lecturas)
        almacen.registrar(obs)
        _csv_compatible(ruta_csv, p, obs)

        est = res.estado
        est.update(fallos=0, ultima_lectura=iso(obs.fecha), ultimo_intento=iso(ahora_p))
        est.pop("aviso_tecnico_enviado", None)
        for k in ("titulo", "imagen", "categoria", "vendedor"):
            if getattr(obs, k):
                est[k] = getattr(obs, k)
        _log(p, obs, res)

        motivo = res.principal
        if motivo and not almacen.aviso_enviado(p.clave, motivo.tipo, obs.precio_cent,
                                                 desde=obs.fecha - VENTANA_DUPLICADOS):
            t = tarjeta_para(p, obs, res, serie_previa, ext)
            try:
                notificador.enviar(t)
                almacen.registrar_aviso(p.clave, motivo.tipo, obs.precio_cent, "seguimiento",
                                        notificador.nombre, fecha=obs.fecha)
                enviados += 1
            except Exception as exc:  # noqa: BLE001
                print("  [%s] no se pudo enviar el aviso (%s): queda pendiente" % (p.nombre, exc))
                datos = asdict(t)
                datos["comprobado"] = iso(t.comprobado)
                est["aviso_pendiente"] = {"tarjeta": datos}
        almacen.guardar_estado(p.clave, est)
    return enviados


def _log(p: Producto, obs: Observacion, res: reglas.Resultado) -> None:
    if obs.precio_cent is None:
        otra = (" | %s: %s" % (obs.otra_condicion, eur(obs.precio_otra_condicion_cent))
                if obs.precio_otra_condicion_cent else "")
        print("[%s] sin unidades nuevas%s" % (p.nombre, otra))
        return
    cupon = " (cupon %s)" % obs.cupon_texto if obs.cupon_texto else ""
    motivos = ", ".join(m.tipo for m in res.motivos) or "sin avisos"
    print("[%s] %s%s | %s" % (p.nombre, eur(obs.precio_cent), cupon, motivos))
