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

import importlib.util
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

# Si el servidor responde 429 con "Retry-After", esperamos lo que pide (en vez
# de los 5s genericos), siempre que no sea absurdamente largo.
MAX_ESPERA_RETRY_AFTER_SEG = float(os.environ.get("MAX_ESPERA_RETRY_AFTER_SEG", "120"))

# Cuanto esperar antes de reintentar tras un CAPTCHA de Amazon. Reintentar a los
# 5s (lo normal) es lo peor: una segunda peticion inmediata es lo que lo dispara.
ESPERA_CAPTCHA_SEG = float(os.environ.get("ESPERA_CAPTCHA_SEG", "60"))

# Como leer Amazon: "auto" (navegador si hay Playwright), "navegador" o "http".
AMAZON_MODO = os.environ.get("AMAZON_MODO", "auto").lower()
AMAZON_ESPERA_PAGINA_SEG = float(os.environ.get("AMAZON_ESPERA_PAGINA_SEG", "20"))
# Quita la marca de "navegador controlado por software".
AMAZON_ARGS_NAVEGADOR = ["--disable-blink-features=AutomationControlled"]

# User-Agent de navegador: SOLO para Amazon, que lo necesita para servir la
# pagina normal.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# User-Agent propio y honesto para endpoints JSON de Shopify. Aprendido a la
# mala (2026-10-02): con el UA de Chrome de arriba, Shopify respondia
# 429 "local_rate_limited" (Retry-After: 60) en TODAS las comprobaciones
# desde la Raspberry, mientras que con un UA que no finge ser un navegador
# (curl, o el de Python por defecto) la misma URL daba 200. Con un UA unico
# nadie mas comparte nuestro limite.
UA_BOT = "silla-sihoo-bot/1.0 (+https://github.com/balbibots/silla-ergonomica)"

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

class ErrorHTTP(RuntimeError):
    """Respuesta HTTP de error, con lo necesario para entender POR QUE falla
    (estado, Retry-After y el principio del cuerpo) en el log y en el aviso."""

    def __init__(self, status: int, retry_after: str | None, cuerpo: str):
        self.status = status
        try:
            self.retry_after_seg = float(retry_after) if retry_after else None
        except ValueError:
            self.retry_after_seg = None  # puede venir como fecha HTTP: la ignoramos
        partes = ["HTTP %s" % status]
        if retry_after:
            partes.append("Retry-After: %s" % retry_after)
        if cuerpo:
            partes.append("«%s»" % cuerpo[:120])
        super().__init__(" | ".join(partes))


def _descargar(url: str, headers: dict, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        cuerpo = exc.read(300).decode("utf-8", errors="replace")
        cuerpo = " ".join(cuerpo.split())
        raise ErrorHTTP(exc.code, exc.headers.get("Retry-After"), cuerpo) from None


def fetch_shopify(producto: dict) -> tuple[float, bool, str]:
    """Endpoint JSON nativo de Shopify (igual que el bot de Yepoda).
    Robusto: no depende de la maquetacion de la pagina. Se identifica con
    UA_BOT, NO con un UA de navegador (ver el comentario de UA_BOT)."""
    url = producto["url"].rstrip("/") + ".js"
    cuerpo = _descargar(url, {"User-Agent": UA_BOT, "Accept": "application/json"})
    data = json.loads(cuerpo.decode("utf-8"))

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


# --------------------------------------------------------------------------
# Amazon
#
# Amazon protege sus fichas con un sistema anti-bots (cookies ak_bmsc / bm_sv,
# de Akamai): con peticiones HTTP simples sirve una pagina de CAPTCHA (HTTP 200,
# ~3 KB) la mayor parte de las veces. Medido el 2026-10-08 desde la IP de casa:
# cliente HTTP -> ~1 acierto de cada 4 peticiones (en la Raspberry, 2 de 12
# ciclos); Chromium real (Playwright) -> 6 de 6. Por eso el modo por defecto es
# el navegador real; el cliente HTTP queda como alternativa.
# --------------------------------------------------------------------------

# Anclas para sacar el precio, de mas a menos fiable. El HTML renderizado por un
# navegador conserva el JSON embebido, asi que la primera sirve en ambos modos.
_PATRON_PRECIO_AMAZON = re.compile(
    r'"desktop_buybox_group_1":\[\{"displayPrice":"[^"]*","priceAmount":([\d.]+)'
)
# Atributo que Amazon anade por JavaScript en la opcion de compra "NEW".
_PATRON_ATRIBUTO_AMAZON = re.compile(
    r'data-csa-c-buying-option-type="NEW"[^>]*?data-csa-c-price-to-pay="([\d.]+)"'
)
# Texto visible del precio a pagar: <span ...apex-pricetopay-value...><span class="a-offscreen">336,99
_PATRON_VISIBLE_AMAZON = re.compile(
    r'apex-pricetopay-value[^>]*>\s*<span class="a-offscreen">\s*([\d.,]+)'
)


class ErrorCaptcha(RuntimeError):
    """Amazon ha servido su pagina de CAPTCHA en lugar de la ficha."""


def _precio_es(texto: str) -> float:
    """'1.299,00' -> 1299.0 ; '336,99' -> 336.99"""
    return float(texto.replace(".", "").replace(",", "."))


def _extraer_precio_amazon(html: str) -> float | None:
    m = _PATRON_PRECIO_AMAZON.search(html)
    if m:
        return float(m.group(1))
    m = _PATRON_ATRIBUTO_AMAZON.search(html)
    if m:
        return float(m.group(1))
    m = _PATRON_VISIBLE_AMAZON.search(html)
    if m:
        return _precio_es(m.group(1))
    return None


def _es_captcha_amazon(html: str) -> bool:
    return "validateCaptcha" in html or "api-services-support@amazon.com" in html


def _precio_o_error_amazon(html: str) -> float:
    """Devuelve el precio del HTML o lanza el error MAS ESPECIFICO posible
    (captcha / pagina sin precio), para que el aviso explique la causa real."""
    precio = _extraer_precio_amazon(html)
    if precio is not None:
        if precio <= 0:
            raise RuntimeError("Precio sospechoso (%s). Mejor revisarlo a mano." % precio)
        return precio
    if _es_captcha_amazon(html):
        raise ErrorCaptcha(
            "Amazon ha servido un CAPTCHA (anti-bots) en lugar de la ficha del producto."
        )
    titulo = re.search(r"<title>([^<]*)</title>", html)
    raise RuntimeError(
        "La pagina de Amazon no trae el precio (%d bytes, titulo: %r). Puede que el "
        "producto este agotado o que Amazon haya cambiado el diseno de la pagina."
        % (len(html), (titulo.group(1).strip()[:80] if titulo else "sin titulo"))
    )


def fetch_amazon_http(producto: dict) -> tuple[float, bool, str]:
    """Alternativa ligera, sin navegador: sufre muchos CAPTCHA (ver arriba)."""
    cuerpo = _descargar(
        producto["url"],
        {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"},
    )
    precio = _precio_o_error_amazon(cuerpo.decode("utf-8", errors="ignore"))
    return precio, True, producto["nombre"]


def fetch_amazon_navegador(producto: dict) -> tuple[float, bool, str]:
    """Carga la ficha con un Chromium real (Playwright), nuevo y sin cookies en
    cada comprobacion, y lee el precio del HTML ya renderizado."""
    from playwright.sync_api import sync_playwright  # import perezoso: solo si hace falta

    with sync_playwright() as p:
        ruta = os.environ.get("CHROMIUM_PATH")
        if ruta:
            navegador = p.chromium.launch(executable_path=ruta, headless=True,
                                          args=AMAZON_ARGS_NAVEGADOR)
        else:
            try:
                # "chromium" = Chromium completo en el nuevo modo headless, mas
                # parecido a un navegador normal que el headless-shell por defecto.
                navegador = p.chromium.launch(channel="chromium", headless=True,
                                              args=AMAZON_ARGS_NAVEGADOR)
            except Exception:  # noqa: BLE001
                navegador = p.chromium.launch(headless=True, args=AMAZON_ARGS_NAVEGADOR)
        try:
            # El UA por defecto de un navegador headless dice "HeadlessChrome":
            # se lo quitamos (sin inventar nada: es el mismo Chromium).
            pagina_vacia = navegador.new_page()
            ua = pagina_vacia.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            pagina_vacia.close()

            pagina = navegador.new_context(
                user_agent=ua, locale="es-ES", timezone_id="Europe/Madrid",
                viewport={"width": 1366, "height": 768},
            ).new_page()
            pagina.goto(producto["url"], wait_until="domcontentloaded", timeout=45000)

            # La ficha se acaba de pintar con JavaScript: miramos el HTML varias
            # veces hasta que aparezca el precio o se vea que es un CAPTCHA.
            limite = time.monotonic() + AMAZON_ESPERA_PAGINA_SEG
            html = ""
            while True:
                try:
                    html = pagina.content()
                except Exception:  # noqa: BLE001 - navegacion en curso
                    html = ""
                if _extraer_precio_amazon(html) is not None or _es_captcha_amazon(html):
                    break
                if time.monotonic() >= limite:
                    break
                pagina.wait_for_timeout(1000)
        finally:
            navegador.close()

    return _precio_o_error_amazon(html), True, producto["nombre"]


def fetch_amazon(producto: dict) -> tuple[float, bool, str]:
    """Elige el metodo segun AMAZON_MODO: "navegador", "http", o "auto" (por
    defecto: navegador si Playwright esta instalado, si no HTTP).

    No se comprueba disponibilidad de forma fiable aqui (a diferencia de
    Shopify): sin precio puede ser agotado, CAPTCHA o cambio de la pagina. Se
    trata siempre como un error, nunca como "agotado".
    """
    modo = AMAZON_MODO
    if modo == "auto":
        modo = "navegador" if importlib.util.find_spec("playwright") else "http"
    if modo == "navegador":
        return fetch_amazon_navegador(producto)
    return fetch_amazon_http(producto)


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
            if intento >= REINTENTOS:
                break

            espera = ESPERA_ENTRE_REINTENTOS_SEG
            if isinstance(exc, ErrorCaptcha):
                espera = ESPERA_CAPTCHA_SEG
            elif isinstance(exc, ErrorHTTP) and exc.status == 429:
                # Reintentar a los 5s un "demasiadas peticiones" no sirve de
                # nada: hacemos caso al servidor. Si pide esperar mas de lo
                # razonable, no insistimos (mejor fallar y avisar).
                pedida = exc.retry_after_seg
                if pedida is not None and pedida > MAX_ESPERA_RETRY_AFTER_SEG:
                    print("  429 con Retry-After=%.0fs (> %.0fs): no reintento."
                          % (pedida, MAX_ESPERA_RETRY_AFTER_SEG))
                    break
                if pedida is not None:
                    espera = pedida + 1  # un segundo de margen

            print("  reintento %d/%d tras fallo (%s), esperando %.0fs..."
                  % (intento, REINTENTOS - 1, exc, espera))
            time.sleep(espera)
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
