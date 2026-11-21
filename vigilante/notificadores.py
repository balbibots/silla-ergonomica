"""Transporte de las tarjetas: Telegram, Discord o consola (modo prueba).

El canal se elige en la configuracion ("canal"). Si el canal elegido no esta
configurado (faltan sus secretos), el bot no arranca: nunca cambia de canal
por su cuenta ni envia a los dos a la vez.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .tarjeta import Tarjeta, a_discord, a_telegram, a_texto, boton_telegram

ESPERA_MAX_429_SEG = 60


class ErrorEnvio(RuntimeError):
    pass


def _post_json(url: str, cuerpo: dict, timeout: float = 20) -> tuple[int, dict, dict]:
    datos = json.dumps(cuerpo).encode("utf-8")
    req = urllib.request.Request(url, data=datos, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            texto = r.read().decode("utf-8") or "{}"
            return r.status, json.loads(texto), dict(r.headers)
    except urllib.error.HTTPError as exc:
        texto = exc.read().decode("utf-8", "replace") or "{}"
        try:
            cuerpo_err = json.loads(texto)
        except json.JSONDecodeError:
            cuerpo_err = {"description": texto[:300]}
        return exc.code, cuerpo_err, dict(exc.headers)


class Consola:
    nombre = "consola"

    def __init__(self, salida=print):
        self.salida = salida
        self.enviadas: list[Tarjeta] = []

    def enviar(self, t: Tarjeta) -> None:
        self.enviadas.append(t)
        self.salida("\n" + "─" * 48 + "\n" + a_texto(t) + "\n" + "─" * 48)


class Telegram:
    """sendPhoto con caption HTML y boton URL; sin foto valida, sendMessage
    con el mismo texto y boton."""

    nombre = "telegram"

    def __init__(self, token: str, chat_id: str, post=_post_json):
        self.base = "https://api.telegram.org/bot%s/" % token
        self.chat_id = chat_id
        self.post = post

    def _llamar(self, metodo: str, cuerpo: dict) -> tuple[int, dict]:
        estado, resp, _ = self.post(self.base + metodo, cuerpo)
        if estado == 429:
            espera = (resp.get("parameters") or {}).get("retry_after", 5)
            if espera > ESPERA_MAX_429_SEG:
                raise ErrorEnvio("Telegram pide esperar %s s: se reintentara en el siguiente ciclo" % espera)
            time.sleep(espera)
            estado, resp, _ = self.post(self.base + metodo, cuerpo)
        return estado, resp

    def enviar(self, t: Tarjeta) -> None:
        texto, teclado = a_telegram(t), boton_telegram(t)
        if t.imagen:
            estado, resp = self._llamar("sendPhoto", {
                "chat_id": self.chat_id, "photo": t.imagen, "caption": texto,
                "parse_mode": "HTML", "reply_markup": teclado})
            if estado == 200 and resp.get("ok"):
                return
            if estado != 400:
                # Un fallo que no es un rechazo explicito puede haber llegado
                # igualmente: no se reenvia como texto para no duplicar.
                raise ErrorEnvio("Telegram sendPhoto: HTTP %s %s" % (estado, resp.get("description", "")))
            print("  Telegram no acepta la foto (%s): se envia sin ella" % resp.get("description", ""))
        estado, resp = self._llamar("sendMessage", {
            "chat_id": self.chat_id, "text": texto, "parse_mode": "HTML",
            "reply_markup": teclado, "link_preview_options": {"is_disabled": True}})
        if estado != 200 or not resp.get("ok"):
            raise ErrorEnvio("Telegram sendMessage: HTTP %s %s" % (estado, resp.get("description", "")))


class Discord:
    """Webhook entrante de un canal privado, con un embed."""

    nombre = "discord"

    def __init__(self, webhook: str, post=_post_json):
        self.url = webhook + ("&" if "?" in webhook else "?") + "wait=true"
        self.post = post

    def enviar(self, t: Tarjeta) -> None:
        cuerpo = a_discord(t)
        estado, resp, cabeceras = self.post(self.url, cuerpo)
        if estado == 429:
            espera = float(resp.get("retry_after") or cabeceras.get("Retry-After") or 5)
            if espera > ESPERA_MAX_429_SEG:
                raise ErrorEnvio("Discord pide esperar %.0f s: se reintentara en el siguiente ciclo" % espera)
            time.sleep(espera)
            estado, resp, _ = self.post(self.url, cuerpo)
        if estado == 400 and t.imagen:
            # Una imagen rechazada no debe hacer perder el aviso.
            cuerpo["embeds"][0].pop("thumbnail", None)
            estado, resp, _ = self.post(self.url, cuerpo)
        if estado not in (200, 204):
            raise ErrorEnvio("Discord: HTTP %s %s" % (estado, resp.get("message", "")))


def crear(canal: str, prueba: bool = False):
    """Notificador del canal configurado. En modo prueba, siempre la consola."""
    if prueba or canal == "consola":
        return Consola()
    if canal == "telegram":
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            raise ErrorEnvio("El canal es telegram pero faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en .env")
        return Telegram(token, chat)
    if canal == "discord":
        webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        if not webhook or not webhook.startswith("https://discord.com/api/webhooks/"):
            raise ErrorEnvio("El canal es discord pero DISCORD_WEBHOOK_URL falta o no es un webhook de Discord")
        return Discord(webhook)
    raise ErrorEnvio("Canal desconocido: %s" % canal)
