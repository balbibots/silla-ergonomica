"""Que observacion merece un aviso.

Solo avisan los acontecimientos relevantes; una comprobacion sin cambios, un
fallo puntual o quedarse sin stock se registran y no se notifican. Si varios
motivos coinciden, sale UN aviso con el motivo principal (el primero de
PRIORIDAD) y los demas como notas.

Definiciones (todas sobre una serie comparable: mismo producto, condicion
"nuevo" y precio de la oferta destacada, sin cupon):

- minimo_registrado: estrictamente por debajo del menor precio valido que el
  bot ha visto desde que empezo a seguirlo. Exige historial suficiente
  (min_observaciones_minimo y min_dias_minimo); la primera lectura nunca es
  un record.
- minimo_igualado: vuelve a ese minimo despues de haber estado por encima.
- minimo_365: por debajo del minimo del grafico de 365 dias de Amazon
  importado a mano, combinado con las lecturas del bot desde esa captura. El
  grafico muestra "el precio actual mas bajo de cada dia" (cualquier oferta),
  que nunca es mayor que el de la oferta destacada: si el precio queda por
  debajo, es un minimo de 365 dias seguro. Solo se usa si la captura no ha
  caducado y el bot no ha dejado huecos de lectura desde entonces.
- objetivo: el precio a pagar (con cupon si lo hay) llega al objetivo por
  primera vez; se rearma cuando vuelve a superarlo.
- bajada: frente a la ultima lectura valida, al menos bajada_pct % O
  bajada_eur EUR.
- reposicion: vuelve a haber oferta NUEVA tras estar sin ella.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .modelo import Observacion, Producto

PRIORIDAD = ["minimo_365", "minimo_registrado", "objetivo", "bajada", "minimo_igualado",
             "reposicion"]
MAX_HUECO_LECTURAS = timedelta(days=2)


@dataclass
class Motivo:
    tipo: str
    datos: dict = field(default_factory=dict)


@dataclass
class Resultado:
    motivos: list[Motivo]
    estado: dict
    media_90_cent: int | None = None

    @property
    def principal(self) -> Motivo | None:
        return self.motivos[0] if self.motivos else None


def media_90_dias(serie: list[tuple[datetime, int]], ahora: datetime,
                  cobertura_min: float = 0.9) -> int | None:
    """Media de las medias diarias de los ultimos 90 dias. Solo si hay lecturas
    en al menos el 90 % de los dias; si no, no se inventa (None)."""
    desde = ahora - timedelta(days=90)
    por_dia: dict = {}
    for f, p in serie:
        if f >= desde:
            por_dia.setdefault(f.date(), []).append(p)
    if len(por_dia) < 90 * cobertura_min:
        return None
    medias = [sum(v) / len(v) for v in por_dia.values()]
    return int(round(sum(medias) / len(medias)))


def minimo_365(ext: tuple[datetime, list[tuple[datetime, int]]] | None,
               serie: list[tuple[datetime, int]], lecturas: list[datetime], ahora: datetime,
               caducidad_dias: float) -> int | None:
    """Minimo de 365 dias verificable, o None si no se puede afirmar."""
    if not ext:
        return None
    captura, puntos = ext
    if ahora - captura > timedelta(days=caducidad_dias):
        return None
    marcas = [captura] + sorted(f for f in lecturas if f >= captura) + [ahora]
    if any(b - a > MAX_HUECO_LECTURAS for a, b in zip(marcas, marcas[1:])):
        return None
    desde = ahora - timedelta(days=365)
    valores = [p for f, p in puntos if f >= desde] + [p for f, p in serie if f >= captura]
    return min(valores) if valores else None


def evaluar(obs: Observacion, producto: Producto, serie_previa: list[tuple[datetime, int]],
            estado: dict, reglas: dict, ahora: datetime,
            ext: tuple[datetime, list[tuple[datetime, int]]] | None = None,
            lecturas: list[datetime] | None = None) -> Resultado:
    """serie_previa: lecturas validas ANTERIORES a obs (misma serie comparable)."""
    estado = dict(estado)
    if not obs.disponible or obs.precio_cent is None:
        estado["disponible"] = False
        return Resultado([], estado)

    precio = obs.precio_cent
    motivos: list[Motivo] = []
    estaba_agotado = estado.get("disponible") is False
    estado["disponible"] = True

    if estaba_agotado and reglas.get("aviso_reposicion", True):
        motivos.append(Motivo("reposicion"))

    objetivo = producto.precio_objetivo_cent
    if objetivo is not None:
        pagar = obs.precio_con_cupon_cent
        if pagar <= objetivo and not estado.get("objetivo_alcanzado"):
            motivos.append(Motivo("objetivo", {"objetivo": objetivo}))
            estado["objetivo_alcanzado"] = True
        elif pagar > objetivo:
            estado["objetivo_alcanzado"] = False

    previo = serie_previa[-1][1] if serie_previa else None
    if previo is not None and precio < previo:
        dif = previo - precio
        pct = producto.bajada_pct if producto.bajada_pct is not None else reglas["bajada_pct"]
        eur = producto.bajada_eur if producto.bajada_eur is not None else reglas["bajada_eur"]
        if dif * 100 >= pct * previo or dif >= eur * 100:
            motivos.append(Motivo("bajada", {"antes": previo}))

    min_prev = min(p for _, p in serie_previa) if serie_previa else None
    if reglas.get("avisar_minimos", True) and min_prev is not None:
        dias = (serie_previa[-1][0] - serie_previa[0][0]).total_seconds() / 86400
        if len(serie_previa) >= reglas["min_observaciones_minimo"] and dias >= reglas["min_dias_minimo"]:
            desde = serie_previa[0][0]
            if precio < min_prev:
                motivos.append(Motivo("minimo_registrado", {"minimo_anterior": min_prev, "desde": desde}))
            elif precio == min_prev and estado.get("subida_tras_minimo"):
                motivos.append(Motivo("minimo_igualado", {"minimo_anterior": min_prev, "desde": desde}))
        m365 = minimo_365(ext, serie_previa, lecturas or [], ahora, reglas["historial_caducidad_dias"])
        if m365 is not None and precio < m365:
            motivos.append(Motivo("minimo_365", {"minimo_anterior": m365, "captura": ext[0]}))
    estado["subida_tras_minimo"] = min_prev is not None and precio > min(min_prev, precio)

    motivos.sort(key=lambda m: PRIORIDAD.index(m.tipo))
    media = media_90_dias(serie_previa + [(obs.fecha, precio)], ahora)
    return Resultado(motivos, estado, media)
