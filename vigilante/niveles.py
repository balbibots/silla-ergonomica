"""Que oportunidad es excepcional: el mismo criterio para todas las fuentes.

Un descuento se mide SIEMPRE frente a una referencia real (precios cobrados
de verdad), en este orden de preferencia:

1. min30      el precio mas bajo de los 30 dias anteriores (Omnibus: lo
              publica Amazon en la ficha, MediaMarkt como precio tachado, o
              sale de la serie de reacofertas)
2. mediano    el "precio mediano" que publica Amazon
3. habitual   el precio habitual de pccomtracker o la mediana diaria de 90
              dias de reacofertas
4. bot        la mediana de lo que el propio bot ha visto (30 dias, al menos
              3 lecturas en 3 dias distintos)

El precio anunciado (PVPR, tachado, "antes" de una publicacion) es un
descuento COMERCIAL. Nunca decide por si solo: si hay una referencia real y
dice que no es para tanto (un -36 % frente al PVPR que es un -8 % frente al
minimo de 30 dias), no hay aviso. Solo si NO hay ninguna referencia real se
puede mandar una alerta PROVISIONAL, y solo para errores y chollazos, con la
lista de lo que falta por confirmar.

Niveles, en cualquier categoria (configurables en descubrimiento.niveles):

- error           80 % o mas, 30 EUR o mas, sobre algo de 50 EUR o mas
- chollazo        60 % o mas y 30 EUR o mas
- extraordinaria  40 % y 50 EUR, o 30 % y 150 EUR

Topes contra datos rotos: una referencia de mas de max_referencia_eur o un
descuento de mas de max_descuento_pct no es una oferta, es un dato mal
cargado (un adaptador "a 12.098 EUR").
"""
from __future__ import annotations

from dataclasses import dataclass, field

ORDEN = ("error", "chollazo", "extraordinaria")
REALES = (("min30", "mínimo de 30 días"), ("mediano", "precio mediano"),
          ("habitual", "precio habitual"), ("bot", "lo visto por el bot"))
POR_DEFECTO = {
    "error": {"descuento_min_pct": 80, "ahorro_min_eur": 30, "valor_min_eur": 50},
    "chollazo": {"descuento_min_pct": 60, "ahorro_min_eur": 30},
    "extraordinaria": {"reglas": [{"descuento_min_pct": 40, "ahorro_min_eur": 50},
                                  {"descuento_min_pct": 30, "ahorro_min_eur": 150}]},
    "max_referencia_eur": 5000,
    "max_descuento_pct": 95,
    "provisional": {"activo": True, "niveles": ["error", "chollazo"],
                    "fuentes": ["amazon_ofertas", "chollomazon", "pccomtracker", "reacofertas"]},
    "realerta_bajada_pct": 5,
}


@dataclass
class Evidencia:
    """Todo lo que se sabe del precio de una oferta. Importes en centimos."""

    precio: int
    min30: int | None = None
    mediano: int | None = None
    habitual: int | None = None
    bot: int | None = None
    anunciado: int | None = None          # PVPR / tachado / "antes": descuento comercial
    anunciado_tipo: str = "PVPR"
    textos: dict = field(default_factory=dict)  # de donde sale cada referencia real

    def real(self) -> tuple[int, str, str] | None:
        """(precio, clave, etiqueta) de la mejor referencia real."""
        for clave, etiqueta in REALES:
            v = getattr(self, clave)
            if v:
                return v, clave, self.textos.get(clave, etiqueta)
        return None


@dataclass
class Veredicto:
    nivel: str                 # error | chollazo | extraordinaria
    pct: float
    ahorro: int
    referencia: int
    referencia_tipo: str       # min30 | mediano | habitual | bot | anunciado
    referencia_texto: str
    provisional: bool = False
    pendiente: list = field(default_factory=list)
    anunciado_pct: float | None = None


def descuento(precio: int, ref: int | None) -> tuple[int, float]:
    if not ref or ref <= precio:
        return 0, 0.0
    return ref - precio, (ref - precio) * 100 / ref


def _nivel(precio: int, ref: int, cfg: dict) -> str | None:
    ahorro, pct = descuento(precio, ref)
    if not ahorro or ref > cfg["max_referencia_eur"] * 100 or pct > cfg["max_descuento_pct"]:
        return None
    e = cfg["error"]
    if pct >= e["descuento_min_pct"] and ahorro >= e["ahorro_min_eur"] * 100 and ref >= e["valor_min_eur"] * 100:
        return "error"
    c = cfg["chollazo"]
    if pct >= c["descuento_min_pct"] and ahorro >= c["ahorro_min_eur"] * 100:
        return "chollazo"
    for r in cfg["extraordinaria"]["reglas"]:
        if pct >= r["descuento_min_pct"] and ahorro >= r["ahorro_min_eur"] * 100:
            return "extraordinaria"
    return None


def evaluar(ev: Evidencia, cfg: dict, fuente: str = "") -> Veredicto | None:
    """El nivel de la oferta, o None. cfg = descubrimiento.niveles."""
    anunciado_pct = descuento(ev.precio, ev.anunciado)[1] or None
    real = ev.real()
    if real:
        ref, tipo, texto = real
        nivel = _nivel(ev.precio, ref, cfg)
        if not nivel:
            return None  # la historia real dice que no es para tanto
        ahorro, pct = descuento(ev.precio, ref)
        return Veredicto(nivel, pct, ahorro, ref, tipo, texto, anunciado_pct=anunciado_pct)
    prov = cfg["provisional"]
    if not (prov["activo"] and ev.anunciado and (not prov["fuentes"] or fuente in prov["fuentes"])):
        return None
    nivel = _nivel(ev.precio, ev.anunciado, cfg)
    if nivel not in prov["niveles"]:
        return None
    ahorro, pct = descuento(ev.precio, ev.anunciado)
    return Veredicto(nivel, pct, ahorro, ev.anunciado, "anunciado", ev.anunciado_tipo, provisional=True,
                     pendiente=["histórico real de precios (mínimo de 30 días o precio habitual)"],
                     anunciado_pct=anunciado_pct)


def mejor(*vs: Veredicto | None) -> Veredicto | None:
    vs = [v for v in vs if v]
    return min(vs, key=lambda v: (v.provisional, ORDEN.index(v.nivel), -v.ahorro)) if vs else None
