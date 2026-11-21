"""El aviso como tarjeta visual, separada del canal que la transporta.

Cada linea solo aparece si el dato existe y esta verificado: no se inventan
medias, minimos ni periodos, y un precio recomendado (PVP) nunca se presenta
como ahorro historico.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime

from .modelo import eur, fecha_corta

# tipo -> (emoji, encabezado, color del borde en Discord)
# Tres niveles que nunca se mezclan: 🏆 minimo historico EXTERNO verificado (el
# grafico de 365 d de Amazon), 📊 minimo del historico LOCAL (lo que ha visto el
# bot) y 🔥 oferta interesante (descuento frente a una referencia, sin historico).
TIPOS = {
    "minimo_365": ("🏆", "Mínimo histórico verificado · 365 días", 0xF1C40F),
    "minimo_registrado": ("📊", "Mínimo del histórico local", 0x3498DB),
    "minimo_igualado": ("🔁", "Vuelve a su mínimo del histórico local", 0x5DADE2),
    "objetivo": ("🎯", "Precio objetivo alcanzado", 0x2ECC71),
    "bajada": ("📉", "Bajada de precio", 0x27AE60),
    "subida": ("📈", "Subida de precio", 0x95A5A6),
    "error_precio": ("🚨", "Posible error de precio", 0xE74C3C),
    "chollo": ("💥", "Chollazo", 0xE67E22),
    "visto_comunidad": ("👀", "Tu producto en Chollometro", 0x1ABC9C),
    "reposicion": ("📦", "De nuevo en stock", 0x9B59B6),
    "oferta": ("🔥", "Oferta interesante", 0xE67E22),
    "interes": ("🛍️", "En tu lista de intereses", 0x16A085),
    "cambio_tienda": ("🔄", "Cambio en una tienda", 0x7F8C8D),
    "aviso_tecnico": ("⚠️", "No puedo leer el precio", 0xE74C3C),
}


@dataclass
class Tarjeta:
    tipo: str
    nombre: str
    url: str
    comprobado: datetime
    precio_cent: int | None = None
    referencia_cent: int | None = None   # se muestra tachada
    referencia_etiqueta: str = ""        # "Media 90 d", "Antes"...
    minimo_anterior_cent: int | None = None
    minimo_periodo: str = ""             # "365 d", "registrado desde 25/10/2026"
    categoria: str | None = None
    disponibilidad: str | None = None    # "✅ En stock · Nuevo"
    fuente: str = ""                     # "Ficha de Amazon"
    condiciones: list[str] = field(default_factory=list)  # "Envío no incluido"...
    notas: list[str] = field(default_factory=list)        # motivos secundarios
    imagen: str | None = None
    asin: str | None = None
    boton: str = "🛒 Ver en Amazon"
    enlaces: list = field(default_factory=list)  # [(texto, url)]: otras tiendas para mirar

    @property
    def emoji(self) -> str:
        return TIPOS[self.tipo][0]

    @property
    def encabezado(self) -> str:
        return TIPOS[self.tipo][1]

    @property
    def color(self) -> int:
        return TIPOS[self.tipo][2]

    @property
    def ahorro(self) -> tuple[int, int] | None:
        """(euros en centimos, porcentaje) frente a la referencia, si es positivo."""
        if self.precio_cent is None or not self.referencia_cent or self.referencia_cent <= self.precio_cent:
            return None
        dif = self.referencia_cent - self.precio_cent
        return dif, round(dif * 100 / self.referencia_cent)

    def lineas(self, negrita, tachado) -> list[str]:
        """Las lineas del cuerpo, con el formato de negrita/tachado del canal."""
        l = []
        if self.precio_cent is not None:
            precio = "💶 " + negrita(eur(self.precio_cent))
            if self.referencia_cent:
                precio += " | %s: %s" % (self.referencia_etiqueta, tachado(eur(self.referencia_cent)))
            l.append(precio)
        if self.ahorro:
            l.append("📉 Ahorro: %s (%d %%)" % (eur(self.ahorro[0]), self.ahorro[1]))
        l.append("")
        if self.minimo_anterior_cent is not None:
            l.append("📊 Mínimo anterior (%s): %s" % (self.minimo_periodo, eur(self.minimo_anterior_cent)))
        if self.categoria:
            l.append("🏷 Categoría: %s" % self.categoria)
        if self.disponibilidad:
            l.append(self.disponibilidad)
        l.append("📅 Comprobado: %s" % fecha_corta(self.comprobado))
        l.extend(self.notas)
        pie = " · ".join(x for x in [self.fuente] + self.condiciones if x)
        if pie:
            l.append(pie)
        while l and l[-1] == "":
            l.pop()
        return l


# ------------------------------------------------------------------- Telegram
LIMITE_CAPTION = 1024


def _telegram(t: Tarjeta) -> str:
    h = html.escape
    # Las marcas de formato van como caracteres de control para que el escape
    # de los datos (nombres con "<", "&"...) no las toque.
    cuerpo = [h(x) for x in t.lineas(lambda s: "\x01%s\x02" % s, lambda s: "\x03%s\x04" % s)]
    texto = "%s <b>%s</b>\n<b>%s</b>\n\n%s" % (t.emoji, h(t.encabezado.upper()), h(t.nombre),
                                              "\n".join(cuerpo))
    for marca, etiqueta in (("\x01", "<b>"), ("\x02", "</b>"), ("\x03", "<s>"), ("\x04", "</s>")):
        texto = texto.replace(marca, etiqueta)
    return texto


def a_telegram(t: Tarjeta) -> str:
    """Texto HTML para caption de sendPhoto (max 1024) o sendMessage. Si no cabe,
    se quitan primero las notas y el pie, y luego se acorta el nombre."""
    texto = _telegram(t)
    if len(texto) <= LIMITE_CAPTION:
        return texto
    t = Tarjeta(**{**t.__dict__, "notas": [], "condiciones": t.condiciones[:1]})
    texto = _telegram(t)
    if len(texto) > LIMITE_CAPTION:
        sobra = len(texto) - LIMITE_CAPTION + 1
        t = Tarjeta(**{**t.__dict__, "nombre": t.nombre[:max(20, len(t.nombre) - sobra)] + "…"})
        texto = _telegram(t)
    return texto


def boton_telegram(t: Tarjeta) -> dict:
    """Boton principal y, debajo, uno por cada tienda que conviene mirar a mano."""
    filas = [[{"text": t.boton, "url": t.url}]]
    otros = [{"text": "🔗 %s" % texto, "url": url} for texto, url in t.enlaces[:4]]
    if otros:
        filas.append(otros[:2])
        if otros[2:]:
            filas.append(otros[2:])
    return {"inline_keyboard": filas}


# -------------------------------------------------------------------- Discord
def a_discord(t: Tarjeta) -> dict:
    """Payload de webhook con un embed. El boton real solo esta permitido a
    webhooks de aplicacion; aqui el titulo enlaza a Amazon y hay un enlace
    visible "🛒 Ver en Amazon"."""
    lineas = t.lineas(lambda s: "**%s**" % s, lambda s: "~~%s~~" % s)
    pie_txt = lineas.pop() if (t.fuente or t.condiciones) else ""
    descripcion = "\n".join(lineas) + "\n\n[%s](%s)" % (t.boton, t.url)
    if t.enlaces:
        descripcion += "\n🔗 Mira también: " + " · ".join("[%s](%s)" % (e[0], e[1]) for e in t.enlaces)
    embed = {
        "title": ("%s %s" % (t.emoji, t.encabezado))[:256],
        "url": t.url,
        "description": ("**%s**\n\n%s" % (t.nombre, descripcion))[:4096],
        "color": t.color,
        "timestamp": t.comprobado.isoformat(),
    }
    if pie_txt:
        embed["footer"] = {"text": pie_txt[:2048]}
    if t.imagen:
        embed["thumbnail"] = {"url": t.imagen}
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


# --------------------------------------------------------------------- Consola
def a_texto(t: Tarjeta) -> str:
    l = t.lineas(lambda s: s, lambda s: "~%s~" % s)
    partes = ["%s %s" % (t.emoji, t.encabezado.upper()), t.nombre, ""] + l
    partes.append("[%s] %s" % (t.boton, t.url))
    for texto, url in t.enlaces:
        partes.append("[🔗 %s] %s" % (texto, url))
    if t.imagen:
        partes.append("(imagen: %s)" % t.imagen)
    return "\n".join(partes)
