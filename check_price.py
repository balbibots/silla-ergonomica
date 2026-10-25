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
  HISTORY_FILE          CSV con todas las lecturas de precio (por defecto,
                        historico_precios.csv junto a STATE_FILE)
  FALLOS_ANTES_DE_AVISAR  ciclos seguidos fallando antes de avisar (def. 3)
  AMAZON_CADA_HORAS     cada cuantas horas se lee Amazon (por defecto 12)
"""
from __future__ import annotations

import contextlib
import copy
import csv
import html
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
HISTORY_FILE = os.environ.get(
    "HISTORY_FILE",
    os.path.join(os.path.dirname(STATE_FILE) or ".", "historico_precios.csv"),
)

# Un fallo suelto (un CAPTCHA, un 429) se arregla casi siempre solo en el
# siguiente ciclo: solo avisamos cuando un producto lleva N ciclos SEGUIDOS sin
# poder leerse (con el bucle de 6 h y N=3, unas 12-18 h fallando).
FALLOS_ANTES_DE_AVISAR = int(os.environ.get("FALLOS_ANTES_DE_AVISAR", "3"))

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

# Como leer Sihoo (Shopify): "auto" (navegador si hay Playwright), "navegador"
# o "http". Ver fetch_shopify_navegador para el por que.
SHOPIFY_MODO = os.environ.get("SHOPIFY_MODO", "auto").lower()

# Como leer Amazon: "auto" (navegador si hay Playwright), "navegador" o "http".
AMAZON_MODO = os.environ.get("AMAZON_MODO", "auto").lower()
AMAZON_ESPERA_PAGINA_SEG = float(os.environ.get("AMAZON_ESPERA_PAGINA_SEG", "20"))
# Menos visitas a Amazon = menos CAPTCHA. Tras una lectura CORRECTA no se vuelve
# a mirar hasta pasadas estas horas; tras un fallo, se reintenta en el siguiente
# ciclo. Para una silla, un precio con unas horas de retraso no cambia nada.
AMAZON_CADA_HORAS = float(os.environ.get("AMAZON_CADA_HORAS", "12"))
# Pausa entre dos fichas de Amazon en el mismo ciclo (no pedirlas de golpe).
ESPERA_ENTRE_AMAZON_SEG = float(os.environ.get("ESPERA_ENTRE_AMAZON_SEG", "60"))
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
# "categoria": agrupa los productos en el mensaje de Telegram (Sillas,
#   Aspiradoras...). Una categoria nueva funciona sola; si quieres darle un
#   emoji, anadelo en EMOJI_CATEGORIA (si no, sale con 📦).
# "nombre" + "tienda": como aparece en los mensajes ("Sihoo B100 blanca" en
#   "Amazon"). El "id" es la memoria del producto: NO lo cambies.
# "umbral_eur": te avisa la primera vez que el precio baje de ese numero.
#   En Amazon el precio vigilado es el FINAL: si la ficha tiene un cupon
#   ("Aplicar cupon de 45 EUR"), ya va descontado.
# Por defecto el umbral es el precio del dia en que se anadio el producto,
# para que te avise en cuanto baje AUNQUE SEA UN CENTIMO. Sube el numero si
# solo quieres que te avise a partir de una rebaja mas grande.
# --------------------------------------------------------------------------
PRODUCTOS = [
    {
        "id": "silla_b100_oficial",
        "categoria": "Sillas",
        "nombre": "Sihoo B100 blanca",
        "tienda": "Sihoo (tienda oficial)",
        "tipo": "shopify",
        "url": "https://eu.sihoo.com/es/products/sihoo-b100-ergonomic-office-chair-with-flip-up-armrest",
        "variant_id": "48150078357718",  # Blanco / Standard
        "umbral_eur": 199.99,
    },
    {
        "id": "silla_b100_amazon",
        "categoria": "Sillas",
        "nombre": "Sihoo B100 blanca",
        "tienda": "Amazon",
        "tipo": "amazon",
        "url": "https://www.amazon.es/SIHOO-B100-Oficina-Soporte-Adaptable/dp/B0G5WQ65P2",
        "umbral_eur": 169.99,
    },
    {
        "id": "silla_flexispot_ergox_lite_amazon",
        "categoria": "Sillas",
        "nombre": "Flexispot ErgoX Lite beige",
        "tienda": "Amazon",
        "tipo": "amazon",
        "url": "https://www.amazon.es/dp/B0H3ZVDKYV",
        # 2026-10-24: 275,49 EUR con un cupon de 45 EUR -> 230,49 EUR.
        "umbral_eur": 230.49,
    },
    {
        "id": "aspiradora_xiaomi_g20_amazon",
        "categoria": "Aspiradoras",
        "nombre": "Xiaomi G20",
        "tienda": "Amazon",
        "tipo": "amazon",
        "url": "https://www.amazon.es/dp/B0DJ82JKFL",
        "umbral_eur": 125.00,  # 2026-10-25 (precio de referencia: 199,99)
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
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"}
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


def _parsear_shopify(data: dict, producto: dict) -> tuple[float, bool, str]:
    """Saca precio y stock de NUESTRA variante del JSON de producto de Shopify."""
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


def fetch_shopify_http(producto: dict) -> tuple[float, bool, str]:
    """Endpoint JSON nativo de Shopify (igual que el bot de Yepoda), pedido
    directamente con urllib e identificandose con UA_BOT."""
    url = producto["url"].rstrip("/") + ".js"
    cuerpo = _descargar(url, {"User-Agent": UA_BOT, "Accept": "application/json"})
    return _parsear_shopify(json.loads(cuerpo.decode("utf-8")), producto)


# El JSON de producto se pide DESDE la propia pagina, con el fetch() del
# navegador: es la misma peticion que hace el JavaScript de la tienda.
_JS_PEDIR_JSON = """async (url) => {
    const r = await fetch(url, {headers: {"Accept": "application/json"}});
    return {status: r.status, retry: r.headers.get("retry-after"), texto: await r.text()};
}"""


def fetch_shopify_navegador(producto: dict) -> tuple[float, bool, str]:
    """Abre la pagina normal del producto con el navegador real, como una
    persona, y lee el precio del JSON de producto de la tienda.

    Por que (2026-10-25): pidiendo el JSON directamente, Sihoo respondia 429
    "too many requests" de forma intermitente (una peticion cada 6 h, desde
    la misma IP unas veces si y otras no, con urllib y con curl), sin causa
    clara. En vez de ir probando clientes hasta dar con uno que no limite, se
    lee como lo leeria cualquier visitante: con un navegador."""
    url_variante = "%s?variant=%s" % (producto["url"].rstrip("/"), producto["variant_id"])
    with _pagina_navegador() as pagina:
        resp = pagina.goto(url_variante, wait_until="domcontentloaded", timeout=45000)
        if resp is not None and resp.status >= 400:
            raise ErrorHTTP(resp.status, resp.headers.get("retry-after"), "pagina del producto")
        r = pagina.evaluate(_JS_PEDIR_JSON, producto["url"].rstrip("/") + ".js")
    if r["status"] >= 400:
        raise ErrorHTTP(r["status"], r["retry"], " ".join(r["texto"][:300].split()))
    return _parsear_shopify(json.loads(r["texto"]), producto)


def _modo(variable: str) -> str:
    """"auto" -> "navegador" si Playwright esta instalado, si no "http"."""
    if variable == "auto":
        return "navegador" if importlib.util.find_spec("playwright") else "http"
    return variable


def fetch_shopify(producto: dict) -> tuple[float, bool, str]:
    modo = _modo(SHOPIFY_MODO)
    print("  Sihoo: leyendo con %s" % ("navegador real (Chromium)" if modo == "navegador" else "peticion HTTP"))
    if modo == "navegador":
        return fetch_shopify_navegador(producto)
    return fetch_shopify_http(producto)


# --------------------------------------------------------------------------
# Navegador real (Playwright), comun a Sihoo y Amazon
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _pagina_navegador():
    """Chromium nuevo y sin cookies, con una pagina lista para usar; se
    cierra siempre al salir."""
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

            yield navegador.new_context(
                user_agent=ua, locale="es-ES", timezone_id="Europe/Madrid",
                viewport={"width": 1366, "height": 768},
            ).new_page()
        finally:
            navegador.close()


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


# Cupon de la ficha: <span ... class="couponLabelText"> Aplicar cupón de 45€
# (o "del 10 %"). Solo aparece en la buybox del propio producto.
_PATRON_CUPON_AMAZON = re.compile(
    r'class="couponLabelText">\s*Aplicar cup(?:ó|o|&oacute;)n de(?:l)?\s*([\d.,]+)\s*(?:&nbsp;|\s)*(€|%|&euro;)'
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


def _cupon_amazon(html: str, precio: float) -> tuple[float, str | None]:
    """Aplica el cupon de la ficha, si lo hay. Devuelve (precio_final, texto
    del cupon o None)."""
    m = _PATRON_CUPON_AMAZON.search(html)
    if not m:
        return precio, None
    try:
        valor = _precio_es(m.group(1))
    except ValueError:
        return precio, None
    if m.group(2) == "%":
        final, texto = precio * (1 - valor / 100.0), "%g %%" % valor
    else:
        final, texto = precio - valor, _eur(valor)
    if final <= 0 or final >= precio:
        return precio, None  # algo raro: mejor ignorar el cupon que inventar
    return round(final, 2), texto


def _es_captcha_amazon(html: str) -> bool:
    return "validateCaptcha" in html or "api-services-support@amazon.com" in html


def _asin(url: str) -> str | None:
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    return m.group(1) if m else None


def _precio_o_error_amazon(html: str, asin: str | None = None) -> float:
    """Devuelve el precio del HTML o lanza el error MAS ESPECIFICO posible
    (captcha / pagina sin precio), para que el aviso explique la causa real."""
    precio = _extraer_precio_amazon(html)
    if precio is not None:
        if precio <= 0:
            raise RuntimeError("Precio sospechoso (%s). Mejor revisarlo a mano." % precio)
        # Defensa extra: que el precio sea de NUESTRA ficha y no de otra (una
        # redireccion a otra variante, una pagina de "productos similares"...).
        if asin and asin not in html:
            raise RuntimeError(
                "La pagina de Amazon trae un precio, pero no es la ficha del ASIN %s." % asin
            )
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


def _resultado_amazon(html: str, producto: dict) -> tuple:
    """(precio_final, disponible, titulo, detalle). El detalle explica el
    precio cuando hay cupon: {"precio_sin_cupon": 275.49, "cupon": "45.00 €"}."""
    base = _precio_o_error_amazon(html, _asin(producto["url"]))
    final, cupon = _cupon_amazon(html, base)
    detalle = {"precio_sin_cupon": base, "cupon": cupon} if cupon else {}
    return final, True, producto["nombre"], detalle


def fetch_amazon_http(producto: dict) -> tuple:
    """Alternativa ligera, sin navegador: sufre muchos CAPTCHA (ver arriba)."""
    cuerpo = _descargar(
        producto["url"],
        {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"},
    )
    return _resultado_amazon(cuerpo.decode("utf-8", errors="ignore"), producto)


def fetch_amazon_navegador(producto: dict) -> tuple:
    """Carga la ficha con un Chromium real (Playwright), nuevo y sin cookies en
    cada comprobacion, y lee el precio del HTML ya renderizado."""
    with _pagina_navegador() as pagina:
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

    return _resultado_amazon(html, producto)


def fetch_amazon(producto: dict) -> tuple:
    """Elige el metodo segun AMAZON_MODO: "navegador", "http", o "auto" (por
    defecto: navegador si Playwright esta instalado, si no HTTP).

    No se comprueba disponibilidad de forma fiable aqui (a diferencia de
    Shopify): sin precio puede ser agotado, CAPTCHA o cambio de la pagina. Se
    trata siempre como un error, nunca como "agotado".
    """
    modo = _modo(AMAZON_MODO)
    print("  Amazon: leyendo con %s" % ("navegador real (Chromium)" if modo == "navegador" else "peticion HTTP"))
    if modo == "navegador":
        return fetch_amazon_navegador(producto)
    return fetch_amazon_http(producto)


FETCHERS = {"shopify": fetch_shopify, "amazon": fetch_amazon}


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Mensajes de Telegram
#
# En cada comprobacion se manda UN SOLO mensaje con todas las novedades,
# agrupadas por categoria y con un bloque por producto. Asi, con muchos
# productos, se sigue leyendo de un vistazo.
# --------------------------------------------------------------------------

EMOJI_CATEGORIA = {"Sillas": "🪑", "Aspiradoras": "🧹"}
SEPARADOR = "━━━━━━━━━━━━━━━"
MAX_LARGO_MENSAJE = 3800  # Telegram corta en 4096


def _h(texto) -> str:
    """Escapa texto para el HTML de Telegram (un error con '<' lo romperia)."""
    return html.escape(str(texto), quote=False)


def _eur(valor: float) -> str:
    """1299.5 -> '1.299,50 €' (formato espanol)."""
    return ("{:,.2f}".format(valor).replace(",", "X").replace(".", ",").replace("X", ".")) + " €"


def _etiqueta(producto: dict) -> str:
    """Para logs e historico: 'Sihoo B100 blanca · Amazon'."""
    return "%s · %s" % (producto["nombre"], producto.get("tienda", producto["tipo"]))


def _bloque(producto: dict, emoji: str, lineas: list[str], disponible: bool | None = None) -> str:
    """Un producto: cabecera, lineas de detalle y pie con stock y enlace."""
    cabecera = "%s <b>%s</b> · %s" % (emoji, _h(producto["nombre"]), _h(producto.get("tienda", "")))
    pie = '<a href="%s">Ver producto</a>' % html.escape(producto["url"], quote=True)
    if disponible is not None and producto["tipo"] == "shopify":  # Amazon no lo detecta fiable
        pie = ("✅ Disponible" if disponible else "❌ Agotado") + "  ·  " + pie
    return "\n".join([cabecera] + lineas + [pie])


def _categorias(bloques: list[tuple[dict, str]]) -> list[str]:
    """Agrupa los bloques por categoria (en el orden de PRODUCTOS) y devuelve
    una seccion de texto por categoria."""
    orden = []
    for p in PRODUCTOS:
        cat = p.get("categoria", "Otros")
        if cat not in orden:
            orden.append(cat)
    secciones = []
    for cat in orden + sorted({p.get("categoria", "Otros") for p, _ in bloques} - set(orden)):
        suyos = [b for p, b in bloques if p.get("categoria", "Otros") == cat]
        if suyos:
            titulo = "%s <b>%s</b>\n%s" % (EMOJI_CATEGORIA.get(cat, "📦"), _h(cat.upper()), SEPARADOR)
            secciones.append(titulo + "\n\n" + "\n\n".join(suyos))
    return secciones


def componer_mensajes(titulo: str, bloques: list[tuple[dict, str]]) -> list[str]:
    """Titulo + secciones por categoria. Si no cabe en un mensaje de Telegram,
    se parte por categorias (cada trozo repite el titulo)."""
    secciones = _categorias(bloques)
    mensajes, actual = [], titulo
    for sec in secciones:
        candidato = actual + "\n\n\n" + sec
        if len(candidato) > MAX_LARGO_MENSAJE and actual != titulo:
            mensajes.append(actual)
            candidato = titulo + "\n\n\n" + sec
        actual = candidato
    mensajes.append(actual)
    return mensajes


def handle_error(estado_producto: dict, producto: dict, mensaje: str) -> str | None:
    """Fallo al leer un producto. Solo genera aviso si lleva
    FALLOS_ANTES_DE_AVISAR ciclos seguidos fallando, y maximo 1 vez/24h.
    Devuelve el bloque del mensaje, o None si no toca avisar."""
    fallos = estado_producto.get("consecutive_failures", 0) + 1
    estado_producto["consecutive_failures"] = fallos
    print("ERROR (%s), fallo %d seguido: %s" % (_etiqueta(producto), fallos, mensaje))
    if fallos < FALLOS_ANTES_DE_AVISAR:
        print("  (aun no aviso: lo hare si sigue fallando %d ciclo(s) mas)"
              % (FALLOS_ANTES_DE_AVISAR - fallos))
        return None

    ahora = datetime.now(timezone.utc)
    anterior = estado_producto.get("last_error_notified_at")
    if anterior:
        try:
            if ahora - datetime.fromisoformat(anterior) <= timedelta(hours=ERROR_REMINDER_HOURS):
                return None
        except ValueError:
            pass

    estado_producto["last_error_notified_at"] = ahora.isoformat()
    return _bloque(producto, "⚠️", [
        "<b>No he podido leer el precio</b> (%d %s)"
        % (fallos, "comprobación" if fallos == 1 else "comprobaciones seguidas"),
        "<code>%s</code>" % _h(mensaje[:300]),
        "<i>No te fíes de su silencio hasta que se arregle.</i>",
    ])


def fetch_con_reintento(fetcher, producto: dict) -> tuple:
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


def toca_comprobar(producto: dict, estado_producto: dict, ahora: datetime) -> bool:
    """Amazon solo se lee cada AMAZON_CADA_HORAS desde la ultima lectura
    CORRECTA (si la ultima fallo, se reintenta ya). El resto, siempre."""
    if producto["tipo"] != "amazon" or AMAZON_CADA_HORAS <= 0:
        return True
    if estado_producto.get("consecutive_failures"):
        return True
    ultima = estado_producto.get("last_checked_at")
    if not ultima:
        return True
    try:
        pasado = ahora - datetime.fromisoformat(ultima)
    except ValueError:
        return True
    # 30 min de margen: el bucle no cae siempre exactamente a la misma hora.
    return pasado >= timedelta(hours=AMAZON_CADA_HORAS) - timedelta(minutes=30)


def guardar_historico(producto: dict, precio: float, disponible: bool, detalle: dict) -> None:
    """Una fila por lectura correcta, para poder ver la evolucion del precio."""
    try:
        nuevo = not os.path.exists(HISTORY_FILE) or os.path.getsize(HISTORY_FILE) == 0
        with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if nuevo:
                w.writerow(["fecha_utc", "id", "producto", "precio_eur",
                            "disponible", "precio_sin_cupon_eur", "cupon"])
            sin_cupon = detalle.get("precio_sin_cupon")
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        producto["id"], _etiqueta(producto), "%.2f" % precio, disponible,
                        "%.2f" % sin_cupon if sin_cupon is not None else "",
                        detalle.get("cupon") or ""])
    except OSError as exc:
        print("  (no he podido guardar el historico: %s)" % exc)


def procesar_producto(producto: dict, estado_producto: dict) -> str | None:
    """Comprueba un producto, actualiza su estado y devuelve el bloque del
    mensaje de Telegram con sus novedades (None si no hay nada que contar)."""
    fetcher = FETCHERS[producto["tipo"]]

    try:
        precio, disponible, titulo, *extra = fetch_con_reintento(fetcher, producto)
    except Exception as exc:  # noqa: BLE001
        return handle_error(estado_producto, producto, "%s: %s" % (type(exc).__name__, exc))
    detalle = extra[0] if extra else {}

    cupon_log = ""
    if detalle.get("cupon"):
        cupon_log = " (%.2f sin cupon, cupon %s)" % (detalle["precio_sin_cupon"], detalle["cupon"])
    print("[%s] %.2f EUR%s | disponible: %s | umbral: %.2f EUR"
          % (_etiqueta(producto), precio, cupon_log, disponible, producto["umbral_eur"]))
    guardar_historico(producto, precio, disponible, detalle)

    anterior_precio = estado_producto.get("last_price")
    ya_avisado = estado_producto.get("below_threshold_notified", False)
    anterior_disponible = estado_producto.get("last_available")
    anterior_cupon = estado_producto.get("last_coupon")
    estado_producto.pop("last_error_notified_at", None)
    estado_producto.pop("consecutive_failures", None)

    emoji, lineas = None, []
    umbral = producto["umbral_eur"]

    def linea_precio() -> str:
        if anterior_precio is None or anterior_precio == precio:
            return "💰 <b>%s</b>" % _eur(precio)
        dif = precio - anterior_precio
        return "💰 %s → <b>%s</b>  (%s%s)" % (_eur(anterior_precio), _eur(precio),
                                            "+" if dif > 0 else "−", _eur(abs(dif)))

    # Stock (solo Shopify: Amazon no lo detecta de forma fiable)
    if producto["tipo"] == "shopify" and anterior_disponible is not None and disponible != anterior_disponible:
        emoji = "🟢" if disponible else "🛑"
        lineas.append("<b>¡Vuelve a haber stock!</b>" if disponible else "<b>Se ha agotado</b>")

    if precio < umbral and not ya_avisado:
        emoji = "🔥"
        lineas.append("<b>¡Por debajo de tu aviso de %s!</b>" % _eur(umbral))
        lineas.append(linea_precio())
        estado_producto["below_threshold_notified"] = True
    elif precio >= umbral and ya_avisado:
        emoji = "↗️"
        lineas.append("<b>Ha vuelto a subir</b> por encima de %s" % _eur(umbral))
        lineas.append(linea_precio())
        lineas.append("<i>Te aviso otra vez si baja de %s.</i>" % _eur(umbral))
        estado_producto["below_threshold_notified"] = False
    elif NOTIFY_ON_ANY_CHANGE and anterior_precio is not None and precio != anterior_precio:
        # No ha cruzado el umbral, pero el precio SI se ha movido: seguimiento
        # general, no solo avisos de "chollo".
        emoji = "📉" if precio < anterior_precio else "📈"
        lineas.append("<b>Ha %s de precio</b>" % ("bajado" if precio < anterior_precio else "subido"))
        lineas.append(linea_precio())
        lineas.append("🎯 Aviso de chollo: por debajo de %s" % _eur(umbral))
    elif lineas:  # solo cambio de stock
        lineas.append(linea_precio())

    if lineas:
        if detalle.get("cupon"):
            lineas.append("🎟️ Incluye cupón de %s (sin cupón: %s). Márcalo en la ficha al comprar."
                          % (_h(detalle["cupon"]), _eur(detalle["precio_sin_cupon"])))
        elif anterior_cupon:
            lineas.append("🎟️ El cupón de %s ya no aparece." % _h(anterior_cupon))

    estado_producto["last_price"] = precio
    if detalle.get("cupon"):
        estado_producto["last_coupon"] = detalle["cupon"]
    else:
        estado_producto.pop("last_coupon", None)
    estado_producto["last_available"] = disponible
    estado_producto["last_checked_at"] = datetime.now(timezone.utc).isoformat()
    return _bloque(producto, emoji, lineas, disponible) if lineas else None


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

    bloques = []
    for p in PRODUCTOS:
        ep = estado.get(p["id"], {})
        precio = ep.get("last_price")
        lineas = ["💰 <b>%s</b>" % _eur(precio) if precio is not None else "<i>Sin datos todavía</i>"]
        if ep.get("last_coupon"):
            lineas.append("🎟️ Con cupón de %s" % _h(ep["last_coupon"]))
        bloques.append((p, _bloque(p, "•", lineas, ep.get("last_available"))))

    try:
        for texto in componer_mensajes("📡 <b>Sigo vigilando</b>  ·  resumen semanal", bloques):
            send_telegram(texto)
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
    ahora = datetime.now(timezone.utc)
    amazon_leidos = 0
    bloques = []      # (producto, texto) con novedades de este ciclo
    pendientes = {}   # id -> (estado anterior, estado nuevo) de los que avisan

    for producto in PRODUCTOS:
        pid = producto["id"]
        anterior = estado.setdefault(pid, {})
        if not toca_comprobar(producto, anterior, ahora):
            print("[%s] leido hace menos de %.0f h, se salta este ciclo"
                  % (_etiqueta(producto), AMAZON_CADA_HORAS))
            continue
        if producto["tipo"] == "amazon":
            if amazon_leidos and ESPERA_ENTRE_AMAZON_SEG > 0:
                print("  pausa de %.0fs antes de la siguiente ficha de Amazon..." % ESPERA_ENTRE_AMAZON_SEG)
                time.sleep(ESPERA_ENTRE_AMAZON_SEG)
            amazon_leidos += 1

        nuevo = copy.deepcopy(anterior)
        bloque = procesar_producto(producto, nuevo)
        if bloque:
            bloques.append((producto, bloque))
            pendientes[pid] = (anterior, nuevo)
        else:
            estado[pid] = nuevo

    if bloques:
        n = len(bloques)
        titulo = "🔔 <b>%d novedad%s</b>" % (n, "" if n == 1 else "es")
        try:
            for texto in componer_mensajes(titulo, bloques):
                send_telegram(texto)
            enviado = True
        except Exception as exc:  # noqa: BLE001
            print("ERROR: no he podido mandar el mensaje de Telegram: %s" % exc)
            enviado = False
        for pid, (anterior, nuevo) in pendientes.items():
            if enviado:
                estado[pid] = nuevo
            else:
                # No ha llegado: no se marca nada como avisado y se repite en
                # el siguiente ciclo. Solo se conserva la cuenta de fallos.
                if "consecutive_failures" in nuevo:
                    anterior["consecutive_failures"] = nuevo["consecutive_failures"]
                else:
                    anterior.pop("consecutive_failures", None)

    comprobar_heartbeat(estado, bool(bloques))
    save_state(estado)
    return 0


if __name__ == "__main__":
    sys.exit(main())
