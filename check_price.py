#!/usr/bin/env python3
"""
Vigila el precio de VARIOS productos, de fuentes distintas, y avisa por
Telegram. Version generalizada del bot de Yepoda: aqui cada producto tiene
su propio "tipo" de fuente (shopify / amazon), su propio umbral, y su propio
estado, para que anadir un producto nuevo en el futuro sea solo anadir una
entrada a PRODUCTOS, no escribir un script nuevo.

Solo usa la libreria estandar: no hay que instalar nada.

Como con Yepoda: no hay avisos de "sigue todo igual" en cada ejecucion, solo
cuando pasa algo real, mas un "sigo vivo" semanal combinado (un solo mensaje
para todos los productos, no uno por producto).

Variables de entorno:
  TELEGRAM_BOT_TOKEN    (obligatoria) token que te da @BotFather
  TELEGRAM_CHAT_ID      (obligatoria) tu chat id
  HEARTBEAT_DAYS        cada cuantos dias mandar el "sigo vivo" (por defecto 7)
  NOTIFY_ON_ANY_CHANGE  "true" avisa de CUALQUIER cambio de precio, no solo
                        cuando cruza el umbral (por defecto "true")
  STATE_FILE            donde se guarda la memoria entre ejecuciones
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HEARTBEAT_DAYS = float(os.environ.get("HEARTBEAT_DAYS", "7"))
NOTIFY_ON_ANY_CHANGE = os.environ.get("NOTIFY_ON_ANY_CHANGE", "true").lower() == "true"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
ERROR_REMINDER_HOURS = 24

# Reintentos ante un fallo de red/bloqueo puntual (sobre todo pensado para
# Amazon, que a veces bloquea una peticion suelta pero no la siguiente unos
# segundos despues). Si TODOS los intentos fallan, se avisa como error.
REINTENTOS = int(os.environ.get("REINTENTOS", "2"))
ESPERA_ENTRE_REINTENTOS_SEG = float(os.environ.get("ESPERA_ENTRE_REINTENTOS_SEG", "5"))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------
# Los productos a vigilar. Anadir uno nuevo = anadir una entrada aqui.
# "umbral_eur": te avisa la primera vez que el precio baje de ese numero.
# Por defecto lo he puesto al precio de hoy (2026-09-27), para que te avise
# en cuanto baje AUNQUE SEA UN CENTIMO de lo que cuesta ahora mismo. Sube el
# numero si solo quieres que te avise a partir de una rebaja mas grande.
# --------------------------------------------------------------------------
PRODUCTOS = [
    {
        "id": "silla_sihoo_oficial",
        "nombre": "Sihoo Doro C300 (tienda oficial, blanca)",
        "tipo": "shopify",
        "url": "https://eu.sihoo.com/es/products/sihoo-doro-c300-ergonomischer-stuhl",
        "variant_id": "44187879375062",  # White / Estandar
        "umbral_eur": 299.99,
    },
    {
        "id": "silla_sihoo_amazon",
        "nombre": "Sihoo Doro C300 (Amazon.es, gris)",
        "tipo": "amazon",
        "url": "https://www.amazon.es/SIHOO-Doro-C300-ergon%C3%B3mica-reposabrazos/dp/B0C3TNC785",
        "umbral_eur": 379.99,
    },
]


# --------------------------------------------------------------------------
# Estado (uno por producto, mas un heartbeat compartido)
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en las variables de entorno."
        )

    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    ).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token, data=payload
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError("Telegram devolvio un error: %s" % body)
    print("  -> aviso enviado a Telegram")


# --------------------------------------------------------------------------
# Lectura del precio: una funcion por tipo de fuente
# --------------------------------------------------------------------------

def fetch_shopify(producto: dict) -> tuple[float, bool, str]:
    """Endpoint JSON nativo de Shopify (igual que el bot de Yepoda).
    Robusto: no depende de la maquetacion de la pagina."""
    url = producto["url"].rstrip("/") + ".js"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError("La web respondio con HTTP %s" % resp.status)
        data = json.loads(resp.read().decode("utf-8"))

    variantes = {str(v["id"]): v for v in data.get("variants", [])}
    variante = variantes.get(str(producto["variant_id"]))
    if variante is None:
        raise RuntimeError(
            "La variante %s ya no existe en la pagina (¿han cambiado el catalogo?)."
            % producto["variant_id"]
        )

    precio = variante["price"] / 100.0
    if precio <= 0:
        raise RuntimeError("Precio sospechoso (%s). Mejor revisarlo a mano." % precio)

    return precio, bool(variante.get("available", False)), data.get("title", producto["nombre"])


# Ancla estable dentro del HTML de Amazon: un bloque JSON embebido con el
# precio de la buybox. Mas fiable que parsear los <span> visibles, pero
# sigue siendo HTML scraping - si Amazon rediseña la pagina, esto puede
# romperse. A diferencia de Shopify, Amazon no tiene un endpoint publico.
_PATRON_PRECIO_AMAZON = re.compile(
    r'"desktop_buybox_group_1":\[\{"displayPrice":"[^"]*","priceAmount":([\d.]+)'
)


def fetch_amazon(producto: dict) -> tuple[float, bool, str]:
    """Scraping del HTML de Amazon. Fragil por diseño: no hay API publica.

    No se comprueba disponibilidad de forma fiable aqui (a diferencia de
    Shopify) - si no se encuentra el precio, puede ser tanto que este
    agotado como que Amazon haya cambiado la pagina o bloqueado la
    peticion. Se trata siempre como un error, nunca como "agotado".
    """
    req = urllib.request.Request(
        producto["url"],
        headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError("Amazon respondio con HTTP %s" % resp.status)
        html = resp.read().decode("utf-8", errors="ignore")

    m = _PATRON_PRECIO_AMAZON.search(html)
    if not m:
        raise RuntimeError(
            "No he podido leer el precio en la pagina de Amazon (puede que "
            "este agotado, que Amazon haya bloqueado la peticion, o que "
            "haya cambiado el diseno de la pagina)."
        )

    precio = float(m.group(1))
    if precio <= 0:
        raise RuntimeError("Precio sospechoso (%s). Mejor revisarlo a mano." % precio)

    return precio, True, producto["nombre"]


FETCHERS = {"shopify": fetch_shopify, "amazon": fetch_amazon}


# --------------------------------------------------------------------------

def handle_error(estado_producto: dict, nombre: str, mensaje: str) -> bool:
    """Avisa de que un producto concreto ha fallado, maximo 1 vez/24h.
    Devuelve True si se ha mandado un aviso nuevo."""
    print("ERROR (%s): %s" % (nombre, mensaje))
    ahora = datetime.now(timezone.utc)
    anterior = estado_producto.get("last_error_notified_at")

    debe_avisar = True
    if anterior:
        try:
            debe_avisar = ahora - datetime.fromisoformat(anterior) > timedelta(hours=ERROR_REMINDER_HOURS)
        except ValueError:
            debe_avisar = True

    if debe_avisar:
        try:
            send_telegram(
                "⚠️ <b>No he podido leer el precio</b>\n\n"
                "<b>%s</b>\n<code>%s</code>\n\n"
                "No te fíes del silencio de este producto hasta que se arregle." % (nombre, mensaje)
            )
            estado_producto["last_error_notified_at"] = ahora.isoformat()
            return True
        except Exception as exc:  # noqa: BLE001
            print("Ademas, no he podido avisar por Telegram: %s" % exc)
    return False


def fetch_con_reintento(fetcher, producto: dict) -> tuple[float, bool, str]:
    """Llama al fetcher del producto, reintentando ante un fallo puntual.

    Pensado sobre todo para Amazon: un bloqueo momentaneo de una peticion
    suelta no significa que la siguiente, unos segundos despues, tambien
    vaya a fallar. Si el ULTIMO intento tambien falla, se relanza ese error
    tal cual (para que el mensaje de aviso sea el real, no uno generico).
    """
    ultimo_error = None
    for intento in range(1, REINTENTOS + 1):
        try:
            return fetcher(producto)
        except Exception as exc:  # noqa: BLE001
            ultimo_error = exc
            if intento < REINTENTOS:
                print("  reintento %d/%d tras fallo (%s), esperando %.0fs..."
                      % (intento, REINTENTOS - 1, exc, ESPERA_ENTRE_REINTENTOS_SEG))
                time.sleep(ESPERA_ENTRE_REINTENTOS_SEG)
    raise ultimo_error


def procesar_producto(producto: dict, estado: dict) -> bool:
    """Comprueba un producto y manda los avisos que correspondan.
    Devuelve True si se ha mandado algun mensaje (para el heartbeat)."""
    pid = producto["id"]
    estado_producto = estado.setdefault(pid, {})
    fetcher = FETCHERS[producto["tipo"]]

    try:
        precio, disponible, titulo = fetch_con_reintento(fetcher, producto)
    except Exception as exc:  # noqa: BLE001
        return handle_error(estado_producto, producto["nombre"], "%s: %s" % (type(exc).__name__, exc))

    print("[%s] %.2f EUR | disponible: %s | umbral: %.2f EUR"
          % (producto["nombre"], precio, disponible, producto["umbral_eur"]))

    anterior_precio = estado_producto.get("last_price")
    ya_avisado = estado_producto.get("below_threshold_notified", False)
    anterior_disponible = estado_producto.get("last_available")
    estado_producto.pop("last_error_notified_at", None)

    algo_enviado = False
    try:
        stock = "✅ disponible" if disponible else "❌ agotado"

        if producto["tipo"] == "shopify" and anterior_disponible is not None and disponible != anterior_disponible:
            if disponible:
                send_telegram("🟢 <b>¡Ha vuelto a haber stock!</b>\n\n<b>%s</b>\n%.2f €\n\n%s"
                              % (producto["nombre"], precio, producto["url"]))
            else:
                send_telegram("🛑 <b>Se ha agotado</b>\n\n<b>%s</b>\nÚltimo precio: %.2f €\n\n%s"
                              % (producto["nombre"], precio, producto["url"]))
            algo_enviado = True

        if precio < producto["umbral_eur"] and not ya_avisado:
            diferencia = ""
            if anterior_precio:
                diferencia = "\nAntes: %.2f €." % anterior_precio
            send_telegram(
                "🔥 <b>¡%s ha bajado de %.0f €!</b>\n\n"
                "Ahora: <b>%.2f €</b>%s\nStock: %s\n\n%s"
                % (producto["nombre"], producto["umbral_eur"], precio, diferencia, stock, producto["url"])
            )
            estado_producto["below_threshold_notified"] = True
            algo_enviado = True

        elif precio >= producto["umbral_eur"] and ya_avisado:
            send_telegram(
                "↗️ <b>%s ha vuelto a subir</b>\n\nAhora: %.2f €\n\n"
                "Te aviso de nuevo si baja de %.0f €." % (producto["nombre"], precio, producto["umbral_eur"])
            )
            estado_producto["below_threshold_notified"] = False
            algo_enviado = True

        elif NOTIFY_ON_ANY_CHANGE and anterior_precio is not None and precio != anterior_precio:
            # No ha cruzado el umbral, pero el precio SI se ha movido (para
            # arriba o para abajo) desde la ultima vez: seguimiento general,
            # no solo avisos de "chollo".
            flecha = "📉" if precio < anterior_precio else "📈"
            send_telegram(
                "%s <b>%s ha cambiado de precio</b>\n\n%.2f € → <b>%.2f €</b>\nStock: %s\n\n"
                "(Tu aviso de chollo sigue puesto en %.0f €.)\n\n%s"
                % (flecha, producto["nombre"], anterior_precio, precio, stock, producto["umbral_eur"], producto["url"])
            )
            algo_enviado = True

    except Exception as exc:  # noqa: BLE001
        # Si el envio falla, no marcamos nada como avisado: se reintenta
        # entero en la siguiente ejecucion.
        print("ERROR: no he podido enviar el aviso de %s: %s" % (producto["nombre"], exc))
        return False

    estado_producto["last_price"] = precio
    estado_producto["last_available"] = disponible
    estado_producto["last_checked_at"] = datetime.now(timezone.utc).isoformat()
    return algo_enviado


def comprobar_heartbeat(estado: dict, hubo_eventos: bool) -> None:
    ahora = datetime.now(timezone.utc)
    anterior = estado.get("last_heartbeat_at")

    if anterior is None:
        # Primera ejecucion: solo fijamos la fecha, sin enviar nada (para no
        # duplicar el --test que ya te llego al configurar el bot).
        estado["last_heartbeat_at"] = ahora.isoformat()
        return

    try:
        toca = ahora - datetime.fromisoformat(anterior) >= timedelta(days=HEARTBEAT_DAYS)
    except ValueError:
        toca = True

    if not toca or hubo_eventos:
        return  # si ya ha pasado algo real esta vez, no duplicamos con un heartbeat

    lineas = ["📡 <b>Sigo vigilando</b>", ""]
    for p in PRODUCTOS:
        ep = estado.get(p["id"], {})
        precio = ep.get("last_price")
        if precio is not None:
            lineas.append("• %s: %.2f €" % (p["nombre"], precio))
        else:
            lineas.append("• %s: sin datos todavía" % p["nombre"])

    try:
        send_telegram("\n".join(lineas))
        estado["last_heartbeat_at"] = ahora.isoformat()
    except Exception as exc:  # noqa: BLE001
        print("ERROR: no he podido enviar el heartbeat: %s" % exc)


# --------------------------------------------------------------------------

def main() -> int:
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        print("ERROR: faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID.")
        return 2

    if "--test" in sys.argv:
        try:
            send_telegram("✅ <b>Prueba correcta</b>\n\nEl bot puede escribirte. Ya está todo conectado.")
        except Exception as exc:  # noqa: BLE001
            print("ERROR: no he podido escribirte: %s" % exc)
            return 2
        return 0

    estado = load_state()
    hubo_eventos = False
    for producto in PRODUCTOS:
        if procesar_producto(producto, estado):
            hubo_eventos = True

    comprobar_heartbeat(estado, hubo_eventos)
    save_state(estado)
    return 0


if __name__ == "__main__":
    sys.exit(main())
