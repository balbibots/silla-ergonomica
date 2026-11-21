"""Pruebas del vigilante. Sin red: fichas sinteticas y canales simulados.

Ejecutar:  python -m unittest discover -s tests -v
"""
import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from vigilante import amazon, config, descubrimiento, fuentes, historial_amazon, notificadores, reglas, seguimiento  # noqa: E402
from vigilante.almacen import Almacen  # noqa: E402
from vigilante.modelo import (Observacion, Producto, a_centimos, asin_desde, eur,  # noqa: E402
                              hora_madrid)
from vigilante.tarjeta import LIMITE_CAPTION, Tarjeta, a_discord, a_telegram  # noqa: E402

ASIN = "B0DJ82JKFL"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def ficha(asin=ASIN, precio="145,00", opciones='"NEW"', extra="", buybox=True):
    """Ficha minima con las mismas marcas que la real."""
    bb = ('"desktop_buybox_group_1":[{"displayPrice":"%s €","priceAmount":%s,' % (
        precio, precio.replace(".", "").replace(",", "."))) if buybox and precio else ""
    return (
        '<title>Xiaomi G20</title><span id="productTitle"> Xiaomi Aspiradora G20 &amp; mas </span>'
        '<img data-old-hires="https://m.media-amazon.com/images/I/abc._AC_SL1000_.jpg" id="landingImage">'
        '<span id="acrPopover" class="x" title="4,1 de 5 estrellas"></span>'
        '<span id="acrCustomerReviewText" aria-label="3.046 Reseñas">(3.046)</span>'
        '<script>{"buyingOptionTypes":[%s],"zipCode":"47195","countryCode":"ES","productAsin":"%s"}</script>'
        '<span class="apex-basisprice-value" data-a-strike="true"><span class="a-offscreen">199,99€</span></span>'
        '<span>O 35,33€ / x4 con Paga en 4</span>%s%s' % (opciones, asin, bb, extra))


class Ficha(unittest.TestCase):
    def test_precio_de_oferta_no_el_tachado_ni_las_cuotas(self):
        o = amazon.analizar_ficha(ficha(), ASIN)
        self.assertEqual(o.precio_cent, 14500)
        self.assertTrue(o.disponible)
        self.assertEqual(o.valoracion, 4.1)
        self.assertEqual(o.num_valoraciones, 3046)
        self.assertEqual(o.titulo, "Xiaomi Aspiradora G20 & mas")
        self.assertIn("_AC_SL1000_", o.imagen)

    def test_precio_con_coma_y_miles(self):
        self.assertEqual(a_centimos("1.299,50"), 129950)
        self.assertEqual(eur(129950), "1.299,50 €")
        o = amazon.analizar_ficha(ficha(precio="1.299,50"), ASIN)
        self.assertEqual(o.precio_cent, 129950)

    def test_otra_variante_o_redireccion(self):
        with self.assertRaises(amazon.ErrorLectura):
            amazon.analizar_ficha(ficha(asin="B000000000"), ASIN)

    def test_captcha_con_http_200_no_es_un_precio(self):
        with self.assertRaises(amazon.ErrorBloqueo):
            amazon.analizar_ficha('<form action="/errors/validateCaptcha">' + ASIN, ASIN)

    def test_sin_precio_es_un_fallo_no_un_agotado(self):
        with self.assertRaises(amazon.ErrorLectura):
            amazon.analizar_ficha(ficha(buybox=False), ASIN)

    def test_solo_segunda_mano_aunque_diga_en_stock(self):
        extra = (' En stock [BuyingOptionData(buyingOptionType=USED, price=137,75&nbsp;€, x)]'
                 '<span class="apex-pricetopay-value"><span class="a-offscreen">137,75</span></span>')
        o = amazon.analizar_ficha(ficha(opciones='"USED"', buybox=False, extra=extra), ASIN)
        self.assertFalse(o.disponible)
        self.assertIsNone(o.precio_cent, "el de 2a mano no entra en el historico de nuevo")
        self.assertEqual((o.otra_condicion, o.precio_otra_condicion_cent), ("usado", 13775))

    def test_reacondicionado(self):
        o = amazon.analizar_ficha(ficha(opciones='"RENEWED_TIER_2"', buybox=False), ASIN)
        self.assertEqual(o.otra_condicion, "reacondicionado")

    def test_agotado_de_verdad(self):
        o = amazon.analizar_ficha(ficha(buybox=False, opciones="", extra='<div id="outOfStock">'), ASIN)
        self.assertFalse(o.disponible)

    def test_cupon(self):
        extra = '<label class="couponLabelText"> Aplicar cupón de 45€ </label>'
        o = amazon.analizar_ficha(ficha(precio="275,49", extra=extra), ASIN)
        self.assertEqual((o.precio_cent, o.cupon_cent, o.precio_con_cupon_cent), (27549, 4500, 23049))


class Utilidades(unittest.TestCase):
    def test_asin_desde_url(self):
        self.assertEqual(asin_desde("https://www.amazon.es/Xiaomi-G20/dp/B0DJ82JKFL/ref=x?th=1"), ASIN)
        self.assertEqual(asin_desde("b0dj82jkfl"), ASIN)
        for malo in ("https://www.amazon.com/dp/B0DJ82JKFL", "hola", "https://www.amazon.es/s?k=x"):
            with self.assertRaises(ValueError):
                asin_desde(malo)

    def test_hora_madrid_invierno_y_verano(self):
        self.assertEqual(hora_madrid(datetime(2026, 1, 10, 12, tzinfo=timezone.utc)).hour, 13)
        self.assertEqual(hora_madrid(datetime(2026, 7, 10, 12, tzinfo=timezone.utc)).hour, 14)
        self.assertEqual(hora_madrid(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)).hour, 2)
        self.assertEqual(hora_madrid(datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)).hour, 2)


REGLAS = {"bajada_pct": 5, "bajada_eur": 10, "aviso_reposicion": True, "avisar_minimos": True,
          "min_observaciones_minimo": 10, "min_dias_minimo": 14, "historial_caducidad_dias": 30}
G20 = Producto(ASIN, "Xiaomi G20", precio_objetivo_cent=12500)


def obs(precio, dias=0, disponible=True):
    return Observacion(ASIN, T0 + timedelta(days=dias), "amazon_ficha", disponible=disponible,
                       precio_cent=precio if disponible else None)


def serie(precios, desde=-30):
    return [(T0 + timedelta(days=desde + i * 2), p) for i, p in enumerate(precios)]


class Reglas(unittest.TestCase):
    def ev(self, o, s, estado=None, **kw):
        return reglas.evaluar(o, G20, s, estado or {}, REGLAS, o.fecha, **kw)

    def test_sin_cambios_no_avisa(self):
        self.assertEqual(self.ev(obs(14500), serie([14500] * 15)).motivos, [])

    def test_bajada_relevante_y_no_relevante(self):
        self.assertEqual(self.ev(obs(14000), serie([14500])).motivos, [], "5 € y 3 %: no basta")
        self.assertEqual(self.ev(obs(13700), serie([14500])).principal.tipo, "bajada")  # 5,5 %
        self.assertEqual(self.ev(obs(250000), serie([262000])).principal.tipo, "bajada")  # 120 €

    def test_primera_lectura_nunca_es_record(self):
        tipos = [m.tipo for m in self.ev(obs(9900), []).motivos]
        self.assertEqual(tipos, ["objetivo"], "solo el objetivo, que ya se cumple; ningun minimo")

    def test_minimo_exige_historial_suficiente(self):
        self.assertNotIn("minimo_registrado", [m.tipo for m in self.ev(obs(13000), serie([14500] * 3)).motivos])
        r = self.ev(obs(13000), serie([14500] * 12))
        self.assertIn("minimo_registrado", [m.tipo for m in r.motivos])

    def test_minimo_igualado_solo_tras_subida(self):
        s = serie([14000] * 11 + [13000])
        r = self.ev(obs(13000, 1), s, {"subida_tras_minimo": False})
        self.assertEqual(r.motivos, [], "sigue en el minimo: nada nuevo")
        r = self.ev(obs(13000, 1), s + [(T0, 14000)], {"subida_tras_minimo": True})
        self.assertIn("minimo_igualado", [m.tipo for m in r.motivos])

    def test_objetivo_una_vez_y_se_rearma(self):
        r1 = self.ev(obs(12400), serie([14500]))
        self.assertIn("objetivo", [m.tipo for m in r1.motivos])
        r2 = self.ev(obs(12300, 1), serie([12400]), r1.estado)
        self.assertNotIn("objetivo", [m.tipo for m in r2.motivos])
        r3 = self.ev(obs(13000, 2), serie([12300]), r2.estado)
        self.assertFalse(r3.estado["objetivo_alcanzado"])

    def test_sin_stock_nuevo_no_avisa_y_al_volver_avisa_una_vez(self):
        r = self.ev(obs(None, disponible=False), serie([14500]))
        self.assertEqual((r.motivos, r.estado["disponible"]), ([], False))
        r2 = self.ev(obs(14500, 1), serie([14500]), r.estado)
        self.assertEqual([m.tipo for m in r2.motivos], ["reposicion"])

    def test_varios_motivos_un_solo_aviso_con_el_principal(self):
        r = self.ev(obs(12000), serie([14500] * 12), {"disponible": False})
        self.assertEqual(r.principal.tipo, "minimo_registrado")
        self.assertEqual({m.tipo for m in r.motivos}, {"minimo_registrado", "objetivo", "bajada", "reposicion"})

    def test_minimo_365_con_historial_valido(self):
        captura = T0 - timedelta(days=5)
        ext = (captura, [(captura - timedelta(days=200), 13900), (captura - timedelta(days=10), 14500)])
        lecturas = [captura + timedelta(hours=12 * i) for i in range(11)]
        s = serie([14500] * 12)
        r = self.ev(obs(13800), s, ext=ext, lecturas=lecturas)
        self.assertEqual(r.principal.tipo, "minimo_365")
        r = self.ev(obs(13900), s, ext=ext, lecturas=lecturas)
        self.assertNotIn("minimo_365", [m.tipo for m in r.motivos], "igualar no es nuevo minimo")

    def test_minimo_365_caducado_o_con_huecos_no_se_afirma(self):
        captura = T0 - timedelta(days=40)
        ext = (captura, [(captura - timedelta(days=100), 13900)])
        self.assertIsNone(reglas.minimo_365(ext, [], [], T0, 30), "captura caducada")
        captura = T0 - timedelta(days=5)
        ext = (captura, [(captura - timedelta(days=100), 13900)])
        self.assertIsNone(reglas.minimo_365(ext, [], [captura + timedelta(days=1)], T0, 30), "hueco de 4 dias")

    def test_media_90_dias_solo_con_cobertura(self):
        diaria = [(T0 - timedelta(days=i), 10000) for i in range(85)]
        self.assertEqual(reglas.media_90_dias(diaria, T0), 10000)
        self.assertIsNone(reglas.media_90_dias(diaria[:40], T0))


def configuracion(tmp, **cambios):
    cfg = config.ejemplo()
    cfg["canal"] = "consola"
    cfg["seguimiento"]["pausa_entre_amazon_seg"] = 0
    for k, v in cambios.items():
        cfg[k] = v
    return cfg


class Seguimiento(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["HISTORY_FILE"] = str(self.tmp / "historico_precios.csv")
        self.almacen = Almacen(self.tmp / "precios.db")
        self.cfg = configuracion(self.tmp)
        self.canal = notificadores.Consola(salida=lambda *_: None)
        self.precio = [14500]

    def lector(self, p):
        if self.precio[0] is None:
            return Observacion(p.clave, T0, "amazon_ficha", disponible=False, otra_condicion="usado",
                               precio_otra_condicion_cent=13775)
        return Observacion(p.clave, T0, "amazon_ficha", precio_cent=self.precio[0],
                           imagen="https://m.media-amazon.com/images/I/a.jpg")

    def vuelta(self, horas):
        return seguimiento.ciclo(self.cfg, self.almacen, self.canal, ahora=T0 + timedelta(hours=horas),
                                 lector=self.lector, dormir=lambda s: None)

    def test_ciclos_sin_cambios_no_avisan_y_respetan_el_intervalo(self):
        for h in range(0, 13 * 10, 13):
            self.vuelta(h)
        self.assertEqual(self.canal.enviadas, [])
        self.vuelta(117 + 1)  # 1 h despues de la ultima lectura: no le toca
        self.assertEqual(len(self.almacen.serie(ASIN)), 10)

    def test_bajada_avisa_una_vez_y_no_se_repite(self):
        self.vuelta(0)
        self.precio = [13000]
        self.vuelta(13)
        self.vuelta(26)
        self.assertEqual([t.tipo for t in self.canal.enviadas], ["bajada"])

    def test_fallo_no_pisa_el_ultimo_precio(self):
        self.vuelta(0)

        def roto(_p):
            raise amazon.ErrorBloqueo("captcha")
        seguimiento.ciclo(self.cfg, self.almacen, self.canal, ahora=T0 + timedelta(hours=13),
                          lector=roto, dormir=lambda s: None)
        self.assertEqual(self.almacen.serie(ASIN)[-1][1], 14500)
        self.assertEqual(self.almacen.estado(ASIN)["fallos"], 1)
        self.assertEqual(len(self.almacen.fallos_recientes(ASIN)), 1)
        self.assertEqual(self.canal.enviadas, [], "avisos tecnicos desactivados por defecto")

    def test_sin_stock_y_reposicion(self):
        self.vuelta(0)
        self.precio = [None]
        self.vuelta(13)
        self.assertEqual(self.canal.enviadas, [])
        self.assertEqual(len(self.almacen.serie(ASIN)), 1, "sin stock no entra en la serie")
        self.precio = [14500]
        self.vuelta(26)
        self.assertEqual([t.tipo for t in self.canal.enviadas], ["reposicion"])

    def test_persistencia_tras_reiniciar(self):
        self.vuelta(0)
        self.precio = [13000]
        self.vuelta(13)
        self.almacen.cerrar()
        self.almacen = Almacen(self.tmp / "precios.db")  # "reinicio de Docker"
        self.vuelta(26)
        self.assertEqual(len(self.canal.enviadas), 1)
        self.assertTrue(self.almacen.aviso_enviado(ASIN, "bajada", 13000))

    def test_aviso_que_no_sale_queda_pendiente_y_se_reintenta(self):
        self.vuelta(0)
        self.precio = [13000]

        class Caido:
            nombre = "telegram"

            def enviar(self, t):
                raise notificadores.ErrorEnvio("sin red")
        seguimiento.ciclo(self.cfg, self.almacen, Caido(), ahora=T0 + timedelta(hours=13),
                          lector=self.lector, dormir=lambda s: None)
        self.assertIn("aviso_pendiente", self.almacen.estado(ASIN))
        self.vuelta(14)  # no le toca leer, pero si reenviar
        self.assertEqual([t.tipo for t in self.canal.enviadas], ["bajada"])
        self.assertNotIn("aviso_pendiente", self.almacen.estado(ASIN))

    def test_csv_compatible_sigue_creciendo(self):
        self.vuelta(0)
        filas = list(csv.DictReader(open(os.environ["HISTORY_FILE"], encoding="utf-8")))
        self.assertEqual((filas[0]["id"], filas[0]["precio_eur"]), (ASIN, "145.00"))


class Migracion(unittest.TestCase):
    def test_csv_idempotente_con_copia(self):
        tmp = Path(tempfile.mkdtemp())
        ruta = tmp / "historico_precios.csv"
        ruta.write_text(
            "fecha_utc,id,producto,precio_eur,disponible,precio_sin_cupon_eur,cupon\n"
            "2026-10-25T20:15:00+00:00,aspiradora_xiaomi_g20_amazon,Xiaomi G20,125.00,True,,\n"
            "2026-11-08T06:08:20+00:00,silla_flexispot_ergox_lite_amazon,Flexi,229.99,True,259.99,\"30,00 €\"\n"
            "2026-10-01T10:00:00+00:00,silla_doro_c300,Doro,150.00,True,,\n"
            "2026-11-21T10:00:00+00:00,B0DJ82JKFL,Xiaomi G20,145.00,True,,\n", encoding="utf-8")
        a = Almacen(tmp / "precios.db")
        r1 = a.migrar_csv(ruta, tmp / "copias")
        r2 = a.migrar_csv(ruta, tmp / "copias")
        self.assertEqual((r1["filas_csv"], r1["nuevas"], r2["nuevas"]), (3, 3, 0))
        self.assertTrue((tmp / "copias" / "historico_precios.csv.antes-de-migrar").exists())
        self.assertEqual(a.serie("B0DJ82JKFL"), [(datetime(2026, 10, 25, 20, 15, tzinfo=timezone.utc), 12500)])
        flexi = a.db.execute("SELECT precio_cent, cupon_cent FROM observaciones WHERE clave='B0H3ZVDKYV'").fetchone()
        self.assertEqual(tuple(flexi), (25999, 3000))
        self.assertEqual(len(a.serie("legacy:silla_doro_c300")), 1, "lo desconocido se conserva")


class Historial(unittest.TestCase):
    def test_importar_har_y_cache(self):
        grafico = {"type": "PriceHistoryChart", "disclaimerLegend": "Excluye envio",
                   "dataPoints": [], "windows": [{"label": "1A", "overrideDataPoints": [
                       {"x": {"value": 200}, "y": {"value": 169.0}},
                       {"x": {"value": 365}, "y": {"value": 219.99}}],
                       "disclaimers": {"belowChart": "precio mas bajo de cada dia"}}]}
        attr = json.dumps({"priceChartJson": json.dumps(grafico)}).replace('"', "&quot;")
        texto = '<span data-rufus-pricing-toggle-action="%s">PriceHistoryChart</span>' % attr
        har = {"log": {"entries": [{"startedDateTime": "2026-11-21T15:29:33.288Z",
                                    "request": {"url": "https://www.amazon.es/rufus/cl/streaming",
                                                "postData": {"text": '{"asin":"%s"}' % ASIN},
                                                "cookies": [{"name": "session-token", "value": "SECRETO"}]},
                                    "response": {"content": {"text": texto}}}]}}
        tmp = Path(tempfile.mkdtemp())
        (tmp / "x.har").write_text(json.dumps(har), encoding="utf-8")
        g = historial_amazon.desde_har(str(tmp / "x.har"), ASIN)
        self.assertEqual([p for _, p in g.puntos], [16900, 21999])
        self.assertEqual(g.puntos[-1][0].date().isoformat(), "2026-11-21")
        self.assertEqual(g.puntos[0][0].date().isoformat(), "2026-06-09")
        a = Almacen(tmp / "p.db")
        a.importar_historial(ASIN, historial_amazon.FUENTE, g.captura, g.puntos)
        self.assertEqual(a.importar_historial(ASIN, historial_amazon.FUENTE, g.captura, g.puntos), 0)
        captura, puntos = a.historial_externo(ASIN, historial_amazon.FUENTE)
        self.assertEqual(len(puntos), 2)
        self.assertNotIn("SECRETO", (tmp / "p.db").read_bytes().decode("latin-1"))
        with self.assertRaises(ValueError):
            historial_amazon.desde_har(str(tmp / "x.har"), "B000000000")


class Tarjetas(unittest.TestCase):
    def tarjeta(self, **kw):
        base = dict(tipo="minimo_365", nombre="Xiaomi <G20> & co", url="https://www.amazon.es/dp/%s" % ASIN,
                    comprobado=T0, precio_cent=12999, referencia_cent=19999, referencia_etiqueta="Media 90 d",
                    minimo_anterior_cent=13999, minimo_periodo="365 d", categoria="Aspiradoras",
                    disponibilidad="✅ En stock · Nuevo", fuente="Fuente: ficha de Amazon",
                    condiciones=["Envío no incluido"], imagen="https://m.media-amazon.com/images/I/a.jpg")
        base.update(kw)
        return Tarjeta(**base)

    def test_telegram_formato_y_escape(self):
        txt = a_telegram(self.tarjeta())
        self.assertIn("<b>129,99 €</b>", txt)
        self.assertIn("<s>199,99 €</s>", txt)
        self.assertIn("Ahorro: 70,00 € (35 %)", txt)
        self.assertIn("Xiaomi &lt;G20&gt; &amp; co", txt)
        self.assertIn("13:00", txt, "hora de Madrid")

    def test_telegram_cabe_en_un_caption(self):
        txt = a_telegram(self.tarjeta(nombre="x" * 2000, notas=["nota larga " * 30]))
        self.assertLessEqual(len(txt), LIMITE_CAPTION)
        self.assertIn("<b>129,99 €</b>", txt)

    def test_sin_referencia_no_inventa_ahorro(self):
        txt = a_telegram(self.tarjeta(referencia_cent=None))
        self.assertNotIn("Ahorro", txt)
        self.assertNotIn("<s>", txt)

    def test_discord_embed(self):
        d = a_discord(self.tarjeta())
        e = d["embeds"][0]
        self.assertEqual(d["allowed_mentions"], {"parse": []})
        self.assertEqual(e["url"], "https://www.amazon.es/dp/%s" % ASIN)
        self.assertIn("**129,99 €**", e["description"])
        self.assertIn("~~199,99 €~~", e["description"])
        self.assertIn("[🛒 Ver en Amazon](https://www.amazon.es/dp/%s)" % ASIN, e["description"])
        self.assertEqual(e["thumbnail"]["url"], "https://m.media-amazon.com/images/I/a.jpg")
        self.assertIn("Envío no incluido", e["footer"]["text"])


class Canales(unittest.TestCase):
    def test_telegram_foto_con_boton(self):
        llamadas = []
        tg = notificadores.Telegram("T", "1", post=lambda u, c: (llamadas.append((u, c)), (200, {"ok": True}, {}))[1])
        tg.enviar(Tarjetas.tarjeta(Tarjetas()))
        self.assertEqual(len(llamadas), 1)
        self.assertTrue(llamadas[0][0].endswith("/sendPhoto"))
        self.assertEqual(llamadas[0][1]["reply_markup"]["inline_keyboard"][0][0]["text"], "🛒 Ver en Amazon")

    def test_telegram_sin_foto_valida_manda_texto(self):
        respuestas = iter([(400, {"ok": False, "description": "wrong file"}, {}), (200, {"ok": True}, {})])
        llamadas = []
        tg = notificadores.Telegram("T", "1", post=lambda u, c: (llamadas.append(u), next(respuestas))[1])
        tg.enviar(Tarjetas.tarjeta(Tarjetas()))
        self.assertEqual([u.rsplit("/", 1)[1] for u in llamadas], ["sendPhoto", "sendMessage"])

    def test_telegram_error_dudoso_no_duplica(self):
        llamadas = []
        tg = notificadores.Telegram("T", "1", post=lambda u, c: (llamadas.append(u), (502, {}, {}))[1])
        with self.assertRaises(notificadores.ErrorEnvio):
            tg.enviar(Tarjetas.tarjeta(Tarjetas()))
        self.assertEqual(len(llamadas), 1, "no reenvia como texto: podria haber llegado")

    def test_discord_respeta_429(self):
        import vigilante.notificadores as n
        respuestas = iter([(429, {"retry_after": 0.01}, {}), (200, {"id": "1"}, {})])
        dc = n.Discord("https://discord.com/api/webhooks/1/abc", post=lambda u, c: next(respuestas))
        dc.enviar(Tarjetas.tarjeta(Tarjetas()))

    def test_canal_sin_configurar_no_arranca(self):
        os.environ.pop("DISCORD_WEBHOOK_URL", None)
        with self.assertRaises(notificadores.ErrorEnvio):
            notificadores.crear("discord")


class Configuracion(unittest.TestCase):
    def test_anadir_quitar_y_duplicados(self):
        cfg = json.loads(config.EJEMPLO.read_text(encoding="utf-8"))
        nuevo, asin = config.anadir_producto(cfg, "https://www.amazon.es/dp/B0G5WQ65P2?th=1", "B100", "Sillas", 150)
        self.assertEqual(asin, "B0G5WQ65P2")
        with self.assertRaises(config.ErrorConfig):
            config.anadir_producto(nuevo, "B0G5WQ65P2", None, None, None)
        self.assertEqual(len(config.quitar_producto(nuevo, "B0G5WQ65P2")["productos"]), 1)

    def test_validacion(self):
        cfg = json.loads(config.EJEMPLO.read_text(encoding="utf-8"))
        for cambio in ({"canal": "email"}, {"productos": [{"asin": "malo", "nombre": "x"}]}):
            with self.assertRaises(config.ErrorConfig):
                config.validar({**cfg, **cambio})
        des = json.loads(json.dumps(cfg["descubrimiento"]))
        des["fuentes"]["chollomazon"]["activo"] = False
        with self.assertRaises(config.ErrorConfig):
            config.validar({**cfg, "descubrimiento": des})
        des["fuentes"]["chollomazon"].update(activo=True, intervalo_min=10)
        with self.assertRaises(config.ErrorConfig, msg="CholloMazon no mas de una vez por hora"):
            config.validar({**cfg, "descubrimiento": des})

    def test_actualiza_la_configuracion_v1_de_la_pi(self):
        tmp = Path(tempfile.mkdtemp())
        v1 = {"canal": "telegram",
              "seguimiento": {"activo": True, "intervalo_min_por_fuente": {"amazon_ficha": 720, "shopify": 360},
                              "pausa_entre_amazon_seg": 60,
                              "reglas": {"bajada_pct": 5, "bajada_eur": 10, "aviso_reposicion": True,
                                         "avisar_minimos": True, "min_observaciones_minimo": 10,
                                         "min_dias_minimo": 14, "historial_caducidad_dias": 30},
                              "avisos_tecnicos": False, "fallos_antes_de_aviso_tecnico": 3},
              "productos": [{"asin": ASIN, "nombre": "Mi G20", "categoria": "Aspiradoras", "precio_objetivo": 120.0}],
              "descubrimiento": {"activo": False, "proveedor": "ninguno", "modo_minimo": "externo",
                                 "intervalo_min": 60, "categorias": ["Electrónica"], "excluir_categorias": [],
                                 "marcas": [], "palabras_clave": [], "excluir_palabras": [], "precio_min": 25,
                                 "precio_max": 600, "descuento_min_pct": 20, "ahorro_min_eur": 10,
                                 "valoracion_min": 4.0, "valoraciones_min": 25, "dias_historial_min": 90,
                                 "solo_nuevo": True, "max_avisos_ejecucion": 5, "max_avisos_dia": 15,
                                 "max_candidatos": 100}}
        ruta = tmp / "config.json"
        ruta.write_text(json.dumps(v1), encoding="utf-8")
        cfg = config.cargar(ruta)
        self.assertEqual(cfg["version"], 3)
        self.assertEqual(cfg["seguimiento"]["intervalo_min_por_fuente"]["amazon_ficha"], 120)
        self.assertEqual(cfg["productos"], v1["productos"], "tus productos no se tocan")
        self.assertEqual(cfg["descubrimiento"]["categorias"], ["Electrónica"], "tus ajustes se conservan")
        self.assertTrue(cfg["descubrimiento"]["activo"])
        self.assertNotIn("proveedor", cfg["descubrimiento"])
        self.assertEqual(cfg["seguimiento"]["reglas"]["historial_caducidad_dias"], 400)
        self.assertTrue((tmp / "config.json.v1.bak").exists())
        self.assertEqual(config.cargar(ruta), cfg, "una segunda carga no cambia nada")


CSV_CM = (
    "titulo,asin,marca,categoria,precio_eur,precio_referencia_eur,descuento_pct,cupon,publicada,termina,comprobada,url\n"
    '"Auriculares X, con ANC",B0AAAAAAA1,Marca: Sony,Electrónica > Auriculares,59.99,129.99,54,,'
    "%(pub)s,,%(comp)s,https://chollomazon.com/oferta/a\n"
    "Sartenes,B0AAAAAAA2,BRA,Hogar > Cocina,55.90,85.50,35,,%(pub)s,,%(comp)s,https://chollomazon.com/oferta/b\n"
    "Camiseta,B0AAAAAAA3,,Moda,15.00,30.00,50,,%(pub)s,,%(comp)s,https://chollomazon.com/oferta/c\n"
    "Antiguo,B0AAAAAAA4,,Electrónica,40.00,90.00,56,,2026-03-01T10:00:00.000Z,,2026-03-01T10:00:00.000Z,https://x\n"
    "Sin referencia,B0AAAAAAA5,,Electrónica,40.00,,0,,%(pub)s,,%(comp)s,https://x\n"
)


class FuenteFalsa:
    nombre = "chollomazon"
    atribucion = fuentes.CHOLLOMAZON_ATRIBUCION

    def __init__(self):
        self.lote = []
        self.error = None

    def candidatos(self):
        if self.error:
            raise self.error
        return list(self.lote)


def lectura_amazon(precio=None, valoracion=4.5, num=300, disponible=True, precios=None):
    """Lector falso de la ficha. Sin precio fijo, devuelve el de la oferta (precios[asin])."""
    def leer(asin):
        p = precio if precio is not None else (precios or {}).get(asin, 5999)
        return Observacion(asin, T0, "amazon_ficha", precio_cent=p if disponible else None,
                           disponible=disponible, valoracion=valoracion, num_valoraciones=num,
                           imagen="https://m.media-amazon.com/images/I/x.jpg", vendedor="Amazon")
    return leer


class CholloMazonCSV(unittest.TestCase):
    def test_parsea_el_formato_real(self):
        texto = CSV_CM % {"pub": "2026-11-20T10:02:15.274Z", "comp": "2026-11-20T10:02:20.434Z"}
        c = fuentes.parsear_chollomazon(texto)
        self.assertEqual(len(c), 5)
        self.assertEqual((c[0].asin, c[0].precio_cent, c[0].precio_referencia_cent), ("B0AAAAAAA1", 5999, 12999))
        self.assertEqual((c[0].marca, c[0].titulo), ("Sony", "Auriculares X, con ANC"))
        self.assertIsNone(c[4].precio_referencia_cent)

    def test_304_y_retry_after(self):
        a = Almacen(":memory:")
        with self.assertRaises(fuentes.SinNovedades):
            fuentes.CholloMazon(a, descargar=lambda: (304, "", {})).candidatos()
        with self.assertRaises(fuentes.FuenteNoDisponible) as ctx:
            fuentes.CholloMazon(a, descargar=lambda: (429, "", {"Retry-After": "7200"})).candidatos()
        self.assertEqual(ctx.exception.reintentar_tras, timedelta(hours=2))
        f = fuentes.CholloMazon(a, descargar=lambda: (200, CSV_CM % {"pub": "", "comp": ""}, {"ETag": '"v1"'}))
        f.candidatos()
        self.assertEqual(a.meta("chollomazon_etag"), '"v1"')
        sin_cambios = fuentes.CholloMazon(a, descargar=lambda: (304, "", {})).candidatos()
        self.assertEqual(len(sin_cambios), 5, "con 304 se revisa la ultima version guardada")


class Descubrimiento(unittest.TestCase):
    def setUp(self):
        self.a = Almacen(":memory:")
        self.cfg = configuracion(None)
        self.cfg["descubrimiento"]["horario"] = None
        self.canal = notificadores.Consola(salida=lambda *_: None)
        self.fuente = FuenteFalsa()

    def lote(self, ahora, **cambios):
        texto = CSV_CM % {"pub": iso_z(ahora - timedelta(minutes=30)), "comp": iso_z(ahora - timedelta(minutes=20))}
        lote = fuentes.parsear_chollomazon(texto)
        for c in lote:
            for k, v in cambios.get(c.asin, {}).items():
                setattr(c, k, v)
        self.fuente.lote = lote

    def ejecutar(self, ahora, leer=None, seguidos=None):
        if leer is None:  # Amazon confirma el precio de la oferta
            leer = lectura_amazon(precios={c.asin: c.precio_cent for c in self.fuente.lote})
        return descubrimiento.ejecutar(self.cfg, self.a, self.canal, [self.fuente], ahora, leer=leer,
                                       dormir=lambda s: None, seguidos=seguidos)

    def test_avisa_ya_de_las_ofertas_activas(self):
        self.lote(T0)
        self.assertEqual(self.ejecutar(T0), 2, "desde la primera descarga, sin esperar")
        tipos = {t.nombre: t.tipo for t in self.canal.enviadas}
        self.assertEqual(tipos, {"Auriculares X, con ANC": "oferta", "Sartenes": "oferta"},
                         "fuera: Moda (categoria), el dato antiguo y el que no tiene referencia")
        t = self.canal.enviadas[0]
        self.assertEqual(t.referencia_etiqueta, "Precio de referencia de Amazon")
        self.assertIn("CholloMazon (CC BY 4.0)", t.fuente)
        self.assertTrue(any("no frente a un histórico" in n for n in t.notas))

    def test_respeta_el_intervalo_de_la_fuente(self):
        self.lote(T0)
        self.ejecutar(T0)
        self.fuente.error = AssertionError("no deberia descargar otra vez tan pronto")
        self.ejecutar(T0 + timedelta(minutes=30))

    def test_verificacion_en_amazon(self):
        self.lote(T0)
        self.assertEqual(self.ejecutar(T0, leer=lectura_amazon(5999, valoracion=3.5)), 0)
        self.lote(T0 + timedelta(hours=1))
        self.assertEqual(self.ejecutar(T0 + timedelta(hours=1), leer=lectura_amazon(5999, disponible=False)), 0)
        filas = self.a.db.execute("SELECT datos FROM candidatos WHERE notificado=0").fetchall()
        motivos = {json.loads(f["datos"])["motivo"] for f in filas}
        self.assertIn("sin unidades nuevas en Amazon", motivos)
        self.assertTrue(any(m.startswith("valoracion") for m in motivos))

    def test_nunca_el_mismo_precio_dos_veces_y_limites(self):
        self.lote(T0)
        self.ejecutar(T0)
        for h in (1, 2, 3):
            self.lote(T0 + timedelta(hours=h))
            self.ejecutar(T0 + timedelta(hours=h))
        self.assertEqual(len(self.canal.enviadas), 2)
        self.cfg["descubrimiento"]["max_avisos_ejecucion"] = 1
        self.lote(T0 + timedelta(hours=4), B0AAAAAAA1={"precio_cent": 4999}, B0AAAAAAA2={"precio_cent": 4500})
        self.assertEqual(self.ejecutar(T0 + timedelta(hours=4)), 1)

    def test_minimo_local(self):
        ahora = T0
        for d in range(0, 40, 4):  # 10 observaciones en 36 dias, siempre a 59,99
            self.lote(ahora + timedelta(days=d))
            self.ejecutar(ahora + timedelta(days=d))
        self.canal.enviadas.clear()
        fin = ahora + timedelta(days=41)
        self.lote(fin, B0AAAAAAA1={"precio_cent": 5499, "publicada": T0})
        self.ejecutar(fin)
        t = self.canal.enviadas[0]
        self.assertEqual((t.tipo, t.minimo_anterior_cent), ("minimo_registrado", 5999))
        self.assertIn("observado por el bot desde", t.minimo_periodo)

    def test_minimo_externo_con_grafico_importado(self):
        self.a.importar_historial("B0AAAAAAA2", historial_amazon.FUENTE, T0 - timedelta(days=3),
                                  [(T0 - timedelta(days=100), 6000), (T0 - timedelta(days=10), 7000)])
        self.lote(T0, B0AAAAAAA2={"precio_cent": 5590})
        self.ejecutar(T0)
        self.assertEqual([t.tipo for t in self.canal.enviadas], ["minimo_365", "oferta"],
                         "el minimo verificado va antes que la oferta interesante")

    def test_producto_seguido_rebajado_pide_lectura_extra(self):
        self.a.registrar(Observacion("B0AAAAAAA2", T0 - timedelta(days=1), "amazon_ficha", precio_cent=7000))
        self.lote(T0)
        self.ejecutar(T0, seguidos={"B0AAAAAAA2"})
        self.assertTrue(self.a.estado("B0AAAAAAA2").get("leer_ya"))
        cfg = configuracion(None)
        cfg["productos"] = [{"asin": "B0AAAAAAA2", "nombre": "Sartenes"}]
        est = self.a.estado("B0AAAAAAA2")
        est["ultimo_intento"] = (T0 - timedelta(hours=2)).isoformat()
        self.a.guardar_estado("B0AAAAAAA2", est)
        leidos = []
        seguimiento.ciclo(cfg, self.a, self.canal, ahora=T0, dormir=lambda s: None,
                          lector=lambda p: (leidos.append(p.clave), Observacion(p.clave, T0, "amazon_ficha", precio_cent=5590))[1])
        self.assertEqual(leidos, ["B0AAAAAAA2"], "se lee aunque no le tocaba por intervalo")
        self.assertNotIn("leer_ya", self.a.estado("B0AAAAAAA2"))


def iso_z(f):
    return f.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
