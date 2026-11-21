"""Modo 1: seguimiento de los productos de la configuracion, en una o varias tiendas.

Cada tienda de cada producto se lee cuando le toca segun el intervalo de su
lector. Cada lectura valida se guarda en la serie de esa tienda; un fallo se
guarda aparte y no toca el ultimo precio bueno. Tras un fallo se reintenta
con esperas crecientes (30 min, 1 h, 2 h...) sin superar el intervalo normal.

Despues, el producto se evalua con el MEJOR precio con stock entre todas sus
tiendas con datos recientes. Las reglas (bajada, objetivo, minimos,
reposicion) se aplican a ese mejor precio:

- la bajada se mide frente al mejor precio de la evaluacion anterior;
- el minimo registrado es el mas bajo visto en cualquiera de sus tiendas;
- la reposicion es que vuelva a haber unidades nuevas en alguna tienda.

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

from . import config, historial_amazon, reglas, tiendas
from .almacen import Almacen
from .modelo import Observacion, Producto, Tienda, ahora_utc, desde_iso, eur, fecha_corta, iso
from .tarjeta import Tarjeta

VENTANA_DUPLICADOS = timedelta(hours=24)
ESPERA_TRAS_FALLO_MIN = 30
# Una lectura de tienda vale para comparar si tiene menos de 2 intervalos + 30 min.
MARGEN_VIGENCIA = timedelta(minutes=30)


def _toca(lector: str, est: dict, seg: dict, ahora: datetime) -> bool:
    ultimo = est.get("ultimo_intento")
    if not ultimo:
        return True
    if est.get("leer_ya"):
        # Lectura extra: el modo descubrimiento lo ha visto rebajado. Con un
        # minimo entre lecturas para no convertirlo en una consulta continua.
        return ahora - desde_iso(ultimo) >= timedelta(minutes=seg["lectura_extra_min_entre"])
    intervalo = seg["intervalo_min_por_fuente"][lector]
    fallos = est.get("fallos", 0)
    if fallos:
        intervalo = min(intervalo, ESPERA_TRAS_FALLO_MIN * 2 ** (fallos - 1))
    # 2 min de margen: el bucle no cae siempre exactamente a la misma hora.
    return ahora - desde_iso(ultimo) >= timedelta(minutes=intervalo - 2)


def leer(t: Tienda, p: Producto) -> Observacion:
    return tiendas.leer(t, p.ean)


def _csv_compatible(ruta: Path, p: Producto, t: Tienda, obs: Observacion) -> None:
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
        nombre = p.nombre if len(p.tiendas) == 1 else "%s · %s" % (p.nombre, t.nombre)
        w.writerow([iso(obs.fecha), t.clave, nombre, "%.2f" % (obs.precio_con_cupon_cent / 100),
                    obs.disponible, "%.2f" % (obs.precio_cent / 100) if obs.cupon_cent else "",
                    obs.cupon_texto or ""])


def _ultima(almacen: Almacen, clave: str):
    return almacen.db.execute("SELECT * FROM observaciones WHERE clave=? AND origen='bot'"
                              " ORDER BY fecha DESC LIMIT 1", (clave,)).fetchone()


def situacion(p: Producto, almacen: Almacen, seg: dict, ahora: datetime) -> list[dict]:
    """Ultimo estado de cada tienda: precio, stock, vendedor y si es reciente."""
    out = []
    for t in p.tiendas:
        fila = _ultima(almacen, t.clave)
        vigencia = timedelta(minutes=2 * seg["intervalo_min_por_fuente"][t.lector]) + MARGEN_VIGENCIA
        reciente = fila is not None and ahora - desde_iso(fila["fecha"]) <= vigencia
        out.append({"tienda": t, "fila": fila, "reciente": reciente,
                    "disponible": bool(reciente and fila["disponible"] and fila["precio_cent"] is not None),
                    "precio": fila["precio_cent"] if fila is not None else None})
    return out


def _serie_producto(almacen: Almacen, p: Producto, hasta: datetime) -> list[tuple[datetime, int]]:
    """Todas las lecturas validas de todas sus tiendas, en orden: el historico
    local del producto."""
    serie = []
    for t in p.tiendas:
        serie.extend(almacen.serie(t.clave, hasta=hasta))
    return sorted(serie)


def tarjeta_para(p: Producto, mejor: dict, sit: list[dict], obs: Observacion, res: reglas.Resultado,
                 serie_previa, anterior, ext) -> Tarjeta:
    motivo = res.principal
    t_best: Tienda = mejor["tienda"]
    fila = mejor["fila"]
    t = Tarjeta(tipo=motivo.tipo, nombre=p.nombre, url=t_best.url, comprobado=obs.fecha,
                precio_cent=obs.precio_cent, categoria=p.categoria, imagen=obs.imagen,
                asin=t_best.clave if t_best.lector == "amazon_ficha" else None,
                boton="🛒 Ver en %s" % t_best.nombre)

    if res.media_90_cent and res.media_90_cent > obs.precio_cent:
        t.referencia_cent, t.referencia_etiqueta = res.media_90_cent, "Media 90 d"
    elif anterior and anterior > obs.precio_cent:
        t.referencia_cent, t.referencia_etiqueta = anterior, "Antes"

    if motivo.tipo == "minimo_365":
        t.minimo_anterior_cent, t.minimo_periodo = motivo.datos["minimo_anterior"], "365 d"
    elif serie_previa:
        desde = motivo.datos.get("desde") or serie_previa[0][0]
        t.minimo_anterior_cent = motivo.datos.get("minimo_anterior") or min(x for _, x in serie_previa)
        alcance = " en %d tiendas" % len(p.tiendas) if len(p.tiendas) > 1 else ""
        t.minimo_periodo = "registrado%s desde %s" % (alcance, fecha_corta(desde)[:10])

    vendido = ", vendido por %s" % fila["vendedor"] if fila["vendedor"] else ""
    t.disponibilidad = "✅ En stock · Nuevo en %s%s" % (t_best.nombre, vendido)
    t.fuente = "Fuente: ficha de %s" % t_best.nombre
    if motivo.tipo == "minimo_365":
        t.fuente += " e historial de 365 d de Amazon (importado el %s, completado con mis lecturas)" \
            % fecha_corta(ext[0])[:10]
    t.condiciones.append("Envío no incluido" if t_best.lector == "amazon_ficha" else "Envío según la tienda")
    if fila["cupon_cent"]:
        t.condiciones.append("Cupón de %s: márcalo en la ficha" % fila["cupon_texto"])

    notas = {
        "reposicion": "📦 Vuelve a haber unidades nuevas",
        "bajada": "📉 Baja desde %s" % eur(anterior) if anterior else "",
        "objetivo": "🎯 Por debajo de tu objetivo de %s" % eur(p.precio_objetivo_cent or 0),
        "minimo_registrado": "📊 También es el mínimo que he registrado",
        "minimo_igualado": "🔁 Iguala su mínimo registrado",
    }
    t.notas = [notas[m.tipo] for m in res.motivos[1:] if notas.get(m.tipo)]
    otras = []
    for s in sit:
        if s["tienda"] is t_best:
            continue
        if s["disponible"]:
            otras.append("%s %s" % (s["tienda"].nombre, eur(s["precio"])))
        elif s["reciente"]:
            otras.append("%s sin stock" % s["tienda"].nombre)
    if otras:
        t.notas.append("🏬 Otras tiendas: " + " · ".join(otras))
    t.enlaces = [(nombre if not nota else "%s (%s)" % (nombre, nota), url) for nombre, url, nota in p.enlaces]
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


def _estado_producto(almacen: Almacen, p: Producto) -> tuple[str, dict]:
    """Estado del producto (aparte del de cada tienda). En los productos que
    vienen del formato antiguo, se trae una vez lo que guardaba su ASIN."""
    clave = "producto:%s" % p.clave
    est = almacen.estado(clave)
    if not est:
        viejo = almacen.estado(p.clave)
        est = {k: viejo[k] for k in ("objetivo_alcanzado", "subida_tras_minimo", "disponible",
                                     "aviso_pendiente") if k in viejo}
    return clave, est


def _leer_tienda(p: Producto, t: Tienda, almacen: Almacen, seg: dict, notificador, lector, ahora_t,
                 ahora_fija, ruta_csv) -> bool:
    """Lee una tienda y guarda el resultado. True si hubo lectura valida."""
    est = almacen.estado(t.clave)
    est["ultimo_intento"] = iso(ahora_t)
    est.pop("leer_ya", None)
    etiqueta = "%s · %s" % (p.nombre, t.nombre)
    try:
        obs = lector(t, p)
    except Exception as exc:  # noqa: BLE001
        est["fallos"] = est.get("fallos", 0) + 1
        almacen.registrar_fallo(t.clave, t.lector, "%s: %s" % (type(exc).__name__, exc), ahora_t)
        print("[%s] fallo %d seguido: %s: %s" % (etiqueta, est["fallos"], type(exc).__name__, exc))
        if seg["avisos_tecnicos"] and est["fallos"] >= seg["fallos_antes_de_aviso_tecnico"] \
                and not est.get("aviso_tecnico_enviado"):
            try:
                notificador.enviar(Tarjeta(tipo="aviso_tecnico", nombre=etiqueta, url=t.url,
                                           comprobado=ahora_t, notas=[str(exc)[:300]]))
                est["aviso_tecnico_enviado"] = True
            except Exception as exc2:  # noqa: BLE001
                print("  no se pudo enviar el aviso tecnico: %s" % exc2)
        almacen.guardar_estado(t.clave, est)
        return False

    obs.clave = t.clave
    obs.fecha = ahora_fija or obs.fecha
    almacen.registrar(obs)
    _csv_compatible(ruta_csv, p, t, obs)
    est.update(fallos=0, ultima_lectura=iso(obs.fecha))
    est.pop("aviso_tecnico_enviado", None)
    for k in ("titulo", "imagen", "vendedor"):
        if getattr(obs, k):
            est[k] = getattr(obs, k)
    almacen.guardar_estado(t.clave, est)
    if obs.precio_cent is None:
        otra = (" | %s: %s" % (obs.otra_condicion, eur(obs.precio_otra_condicion_cent))
                if obs.precio_otra_condicion_cent else "")
        print("[%s] sin unidades nuevas%s" % (etiqueta, otra))
    else:
        vendedor = " (vendido por %s)" % obs.vendedor if obs.vendedor and obs.vendedor != t.nombre else ""
        print("[%s] %s%s" % (etiqueta, eur(obs.precio_cent), vendedor))
    return True


def ciclo(cfg: dict, almacen: Almacen, notificador, ahora: datetime | None = None, lector=leer,
          dormir=time.sleep) -> int:
    """Una pasada por las tiendas a las que les toca. Devuelve avisos enviados."""
    seg = cfg["seguimiento"]
    ruta_csv = Path(os.environ.get("HISTORY_FILE", config.RAIZ / "data" / "historico_precios.csv"))
    enviados, primero_amazon = 0, True
    for p in config.productos(cfg):
        clave_p, est_p = _estado_producto(almacen, p)
        _enviar_pendiente(p.clave, est_p, almacen, notificador)
        inicio = ahora or ahora_utc()
        leidas = 0
        for t in p.tiendas:
            ahora_t = ahora or ahora_utc()
            if not _toca(t.lector, almacen.estado(t.clave), seg, ahora_t):
                continue
            if t.lector == "amazon_ficha":
                if not primero_amazon:
                    dormir(seg["pausa_entre_amazon_seg"])
                primero_amazon = False
            leidas += _leer_tienda(p, t, almacen, seg, notificador, lector, ahora_t, ahora, ruta_csv)
        if not leidas:
            almacen.guardar_estado(clave_p, est_p)
            continue
        enviados += _evaluar(p, clave_p, est_p, almacen, seg, notificador, inicio, ahora or ahora_utc())
    return enviados


def _evaluar(p: Producto, clave_p: str, est_p: dict, almacen: Almacen, seg: dict, notificador,
             inicio: datetime, ahora: datetime) -> int:
    sit = situacion(p, almacen, seg, ahora)
    disponibles = [s for s in sit if s["disponible"]]
    mejor = min(disponibles, key=lambda s: s["precio"]) if disponibles else None
    obs = Observacion(clave=p.clave, fecha=ahora, fuente="producto", disponible=mejor is not None,
                      precio_cent=mejor["precio"] if mejor else None)
    if mejor:
        fila = mejor["fila"]
        obs.cupon_cent, obs.cupon_texto = fila["cupon_cent"], fila["cupon_texto"]
        obs.vendedor = fila["vendedor"]
        imagenes = [almacen.estado(t.clave).get("imagen") for t in [mejor["tienda"]] + p.tiendas]
        obs.imagen = next((i for i in imagenes if i), None)

    serie_previa = _serie_producto(almacen, p, inicio)
    anterior = est_p.get("mejor_precio_cent")
    ext, lecturas = None, []
    if mejor and mejor["tienda"].lector == "amazon_ficha":
        asin = mejor["tienda"].clave
        ext = almacen.historial_externo(asin, historial_amazon.FUENTE)
        if ext:
            lecturas = [desde_iso(r["fecha"]) for r in almacen.db.execute(
                "SELECT fecha FROM observaciones WHERE clave=? AND fecha>=?", (asin, iso(ext[0])))]
    res = reglas.evaluar(obs, p, serie_previa, est_p, seg["reglas"], ahora, ext, lecturas, anterior=anterior)
    claves_tiendas = sorted(t.clave for t in p.tiendas)
    if est_p.get("tiendas") != claves_tiendas:
        # Primera evaluacion con este conjunto de tiendas: una tienda recien
        # anadida que ya tenia stock no es una reposicion.
        res.motivos = [m for m in res.motivos if m.tipo != "reposicion"]
    est_p = res.estado
    est_p["tiendas"] = claves_tiendas
    est_p["mejor_precio_cent"] = obs.precio_cent
    est_p["mejor_tienda"] = mejor["tienda"].nombre if mejor else None
    if mejor and res.motivos:
        print("  [%s] mejor: %s en %s | %s" % (p.nombre, eur(obs.precio_cent), mejor["tienda"].nombre,
                                              ", ".join(m.tipo for m in res.motivos)))

    enviados = 0
    motivo = res.principal
    if motivo and not almacen.aviso_enviado(p.clave, motivo.tipo, obs.precio_cent,
                                             desde=ahora - VENTANA_DUPLICADOS):
        t = tarjeta_para(p, mejor, sit, obs, res, serie_previa, anterior, ext)
        try:
            notificador.enviar(t)
            almacen.registrar_aviso(p.clave, motivo.tipo, obs.precio_cent, "seguimiento",
                                    notificador.nombre, fecha=ahora)
            enviados = 1
        except Exception as exc:  # noqa: BLE001
            print("  [%s] no se pudo enviar el aviso (%s): queda pendiente" % (p.nombre, exc))
            datos = asdict(t)
            datos["comprobado"] = iso(t.comprobado)
            est_p["aviso_pendiente"] = {"tarjeta": datos}
    almacen.guardar_estado(clave_p, est_p)
    return enviados
