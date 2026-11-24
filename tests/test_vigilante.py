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

from vigilante import (amazon, config, descubrimiento, fuentes, fuentes_historico, historial_amazon,  # noqa: E402
                       fuentes_tiendas, historia, juez, niveles, notificadores, reglas, seguimiento, terceros,
                       tiendas)
from vigilante.almacen import Almacen  # noqa: E402
from vigilante.modelo import (Observacion, Producto, a_centimos, asin_desde, eur,  # noqa: E402
                              hora_madrid, iso)
from vigilante.tarjeta import LIMITE_CAPTION, Tarjeta, a_discord, a_telegram, boton_telegram  # noqa: E402

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
    cfg["productos"] = [{"asin": ASIN, "nombre": "Xiaomi G20", "categoria": "Aspiradoras",
                         "precio_objetivo": 125.0}]
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

    def lector(self, p, _producto=None):
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
        self.assertEqual([t.tipo for t in self.canal.enviadas], ["cambio_tienda"])
        self.assertEqual(self.canal.enviadas[0].disponibilidad, "❌ Amazon se queda sin stock (estaba a 145,00 €)")
        self.vuelta(26)
        self.assertEqual(len(self.canal.enviadas), 1, "sigue agotado: nada nuevo que contar")
        self.assertEqual(len(self.almacen.serie(ASIN)), 1, "sin stock no entra en la serie")
        self.precio = [14500]
        self.vuelta(39)
        self.assertEqual([t.tipo for t in self.canal.enviadas], ["cambio_tienda", "reposicion"])

    def test_cada_transicion_avisa_aunque_el_precio_ya_se_viera(self):
        self.vuelta(0)
        for h, precio in ((13, 13000), (26, 14500), (39, 13000), (52, 14500)):
            self.precio = [precio]
            self.vuelta(h)
        self.assertEqual([t.tipo for t in self.canal.enviadas], ["bajada", "subida", "bajada", "subida"])

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
        self.assertIn("aviso_pendiente", self.almacen.estado("producto:" + ASIN))
        self.vuelta(14)  # no le toca leer, pero si reenviar
        self.assertEqual([t.tipo for t in self.canal.enviadas], ["bajada"])
        self.assertNotIn("aviso_pendiente", self.almacen.estado("producto:" + ASIN))

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
        self.assertIn("(−70,00 € · −35 %)", txt)
        self.assertIn("· <b>Xiaomi &lt;G20&gt; &amp; co</b>", txt, "evento y producto en la primera linea")
        self.assertLessEqual(txt.count("\n"), 7, "compacta: se lee de un vistazo")
        self.assertIn("13:00", txt, "hora de Madrid")

    def test_telegram_cabe_en_un_caption(self):
        txt = a_telegram(self.tarjeta(nombre="x" * 2000, notas=["nota larga " * 30]))
        self.assertLessEqual(len(txt), LIMITE_CAPTION)
        self.assertIn("<b>129,99 €</b>", txt)

    def test_sin_referencia_no_inventa_ahorro(self):
        txt = a_telegram(self.tarjeta(referencia_cent=None))
        self.assertNotIn("−35 %", txt)
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
        self.assertEqual(len(config.quitar_producto(nuevo, "B0G5WQ65P2")["productos"]), len(cfg["productos"]))
        with self.assertRaises(config.ErrorConfig, msg="la G20 ya esta, aunque sea en varias tiendas"):
            config.anadir_producto(cfg, "B0DJ82JKFL", None, None, None)
        self.assertEqual(len(config.quitar_producto(cfg, "dreame-u20")["productos"]), len(cfg["productos"]) - 1)

    def test_validacion(self):
        cfg = json.loads(config.EJEMPLO.read_text(encoding="utf-8"))
        for cambio in ({"canal": "email"}, {"productos": [{"asin": "malo", "nombre": "x"}]}):
            with self.assertRaises(config.ErrorConfig):
                config.validar({**cfg, **cambio})
        des = json.loads(json.dumps(cfg["descubrimiento"]))
        des["fuentes"]["chollomazon"]["activo"] = False
        for f in ("chollometro", "pccomtracker", "reacofertas", "amazon_ofertas"):
            des["fuentes"][f]["activo"] = False
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
        self.assertEqual(cfg["version"], config.VERSION)
        self.assertEqual(cfg["seguimiento"]["intervalo_min_por_fuente"]["amazon_ficha"], 120)
        g20 = cfg["productos"][0]
        self.assertEqual((g20["id"], g20["nombre"], g20["precio_objetivo"]), (ASIN, "Mi G20", 120.0),
                         "tu G20 conserva nombre y objetivo")
        self.assertEqual([t["tienda"] for t in g20["tiendas"]], ["Amazon", "Powerplanet", "MediaMarkt",
                                                                 "PcComponentes"])
        self.assertEqual([p["id"] for p in cfg["productos"][1:]], ["dreame-u20", "dreame-r20-se-a"])
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
        # Estas pruebas miden la mecanica con el umbral antiguo (20 %, 3 categorias).
        self.cfg["descubrimiento"]["oferta_interesante"].update(descuento_min_pct=20, ahorro_min_eur=10)
        self.cfg["descubrimiento"]["categorias"] = ["Electrónica", "Informática", "Hogar"]
        self.canal = notificadores.Consola(salida=lambda *_: None)
        self.fuente = FuenteFalsa()
        # El bot ya lleva semanas viendo estos dos: su precio habitual es real.
        self.historia({"B0AAAAAAA1": 12999, "B0AAAAAAA2": 15000})

    def historia(self, precios, dias=(25, 20, 10, 5)):
        for asin, precio in precios.items():
            for d in dias:
                historia.registrar(self.a, historia.clave("Amazon", asin=asin), "amazon_ficha", "Amazon", precio,
                                   T0 - timedelta(days=d), asin=asin, disponible=1)

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

    def test_sin_historico_el_tachado_de_amazon_no_basta(self):
        self.a = Almacen(":memory:")  # sin historia
        self.lote(T0)
        self.assertEqual(self.ejecutar(T0), 0, "54 % frente al precio de referencia de Amazon, pero sin historico "
                                               "(y sin historico solo hay provisionales de errores y chollazos)")

    def test_avisa_ya_de_las_ofertas_activas(self):
        self.lote(T0)
        self.assertEqual(self.ejecutar(T0), 2, "desde la primera descarga, sin esperar")
        tipos = {t.nombre: t.tipo for t in self.canal.enviadas}
        self.assertEqual(tipos, {"Auriculares X, con ANC": "extraordinaria", "Sartenes": "chollo"},
                         "54 % y 70 EUR: extraordinaria; 63 % y 94 EUR: chollazo")
        t = next(t for t in self.canal.enviadas if t.nombre == "Auriculares X, con ANC")
        self.assertEqual(t.notas[0], "✅ Descuento real: −53 % (−70,00 €) frente a lo que he visto en 30 días")
        self.assertEqual((t.referencia_cent, t.referencia_etiqueta), (12999, "habitual"))
        self.assertIn("CholloMazon (CC BY 4.0)", t.fuente)

    def test_respeta_el_intervalo_de_la_fuente(self):
        self.lote(T0)
        self.ejecutar(T0)
        self.fuente.error = AssertionError("no deberia descargar otra vez tan pronto")
        self.ejecutar(T0 + timedelta(minutes=30))

    def test_verificacion_en_amazon(self):
        self.lote(T0)
        self.assertEqual(self.ejecutar(T0, leer=lectura_amazon(5999, valoracion=3.5)), 0)
        self.lote(T0 + timedelta(hours=7))
        self.assertEqual(self.ejecutar(T0 + timedelta(hours=7), leer=lectura_amazon(5999, disponible=False)), 0,
                         "pasadas 6 h se vuelve a comprobar la misma oferta")
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
        self.assertIn("Lo más barato que he visto (59,99 €, observado por el bot desde", t.veredicto)

    def test_error_de_precio_fuera_de_tus_temas_y_de_horario(self):
        self.cfg["descubrimiento"]["horario"] = {"desde": "08:00", "hasta": "09:00"}
        self.lote(T0, B0AAAAAAA3={"precio_referencia_cent": 49900, "precio_cent": 4999})  # Moda, 90 %
        self.ejecutar(T0)
        t = self.canal.enviadas[0]
        self.assertEqual(t.tipo, "error_comprobado", "Amazon confirma precio y stock")
        self.assertTrue(any(n.startswith("✅ Precio y stock comprobados en Amazon") for n in t.notas))

    def test_error_sin_valoraciones_y_sin_comprobar(self):
        self.lote(T0, B0AAAAAAA3={"precio_referencia_cent": 49900, "precio_cent": 4999})
        self.ejecutar(T0, leer=lectura_amazon(4999, valoracion=2.0, num=3))
        self.assertEqual([t.tipo for t in self.canal.enviadas if "Camiseta" in t.nombre], ["error_comprobado"],
                         "las valoraciones no se aplican a los errores de precio")
        self.canal.enviadas.clear()

        def caido(_asin):
            raise RuntimeError("Amazon no responde")
        self.lote(T0 + timedelta(hours=1), B0AAAAAAA3={"precio_referencia_cent": 49900, "precio_cent": 3999})
        self.ejecutar(T0 + timedelta(hours=1), leer=caido)
        t = next(t for t in self.canal.enviadas if "Camiseta" in t.nombre)
        self.assertEqual(t.tipo, "error_precio", "no se ha podido comprobar: sale como sospechoso")
        self.assertTrue(any(n.startswith("⚠️ Sin comprobar") for n in t.notas))

    def test_valoraciones_configurables(self):
        self.cfg["descubrimiento"]["valoraciones_en"] = []
        self.lote(T0)
        self.assertEqual(self.ejecutar(T0, leer=lectura_amazon(precios={"B0AAAAAAA1": 5999, "B0AAAAAAA2": 5590},
                                                               valoracion=3.0, num=2)), 2,
                         "sin exigir valoraciones a los chollazos")

    def test_reacondicionado_y_productos_seguidos_fuera(self):
        self.lote(T0, B0AAAAAAA1={"titulo": "Auriculares X (Reacondicionado)"})
        self.ejecutar(T0, seguidos={"B0AAAAAAA2"})
        self.assertEqual(self.canal.enviadas, [])

    def test_interes_activo_en_cholloMazon(self):
        self.lote(T0, B0AAAAAAA3={"titulo": "Cecotec Conga aspiradora escoba sin cable", "categoria": "Moda",
                                  "precio_cent": 12999, "precio_referencia_cent": 13999})
        self.ejecutar(T0)
        t = next(t for t in self.canal.enviadas if "Cecotec" in t.nombre)
        self.assertEqual((t.tipo, t.categoria), ("interes", "Aspiradoras"))
        self.assertTrue(t.nombre.startswith("Aspiradoras · Cecotec"), "el interes va junto al nombre, una vez")

    def test_minimo_externo_con_grafico_importado(self):
        self.a.importar_historial("B0AAAAAAA2", historial_amazon.FUENTE, T0 - timedelta(days=3),
                                  [(T0 - timedelta(days=100), 6000), (T0 - timedelta(days=10), 7000)])
        self.lote(T0, B0AAAAAAA2={"precio_cent": 5590})
        self.ejecutar(T0)
        self.assertEqual([t.tipo for t in self.canal.enviadas], ["minimo_365", "extraordinaria"],
                         "el minimo verificado va antes que la oferta extraordinaria")

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
                          lector=lambda t, p: (leidos.append(t.clave), Observacion(t.clave, T0, "amazon_ficha", precio_cent=5590))[1])
        self.assertEqual(leidos, ["B0AAAAAAA2"], "se lee aunque no le tocaba por intervalo")
        self.assertNotIn("leer_ya", self.a.estado("B0AAAAAAA2"))


def iso_z(f):
    return f.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


EAN_G20 = "6941812791936"


def ficha_jsonld(precio="159.95", ean=EAN_G20, disponibilidad="InStock", vendedor="Powerplanet",
                 condicion="NewCondition", envoltorio="grupo"):
    """Como la de Powerplanet (ProductGroup.hasVariant) o MediaMarkt (BuyAction.object)."""
    producto = {"@type": "Product", "name": "Xiaomi Vacuum Cleaner G20 <Blanco>", "gtin13": ean,
                "image": ["https://img.example/g20.jpg"],
                "offers": [{"@type": "Offer", "price": precio, "priceCurrency": "EUR",
                            "itemCondition": "https://schema.org/" + condicion,
                            "availability": "https://schema.org/" + disponibilidad,
                            "seller": {"@type": "Organization", "name": vendedor}}]}
    if envoltorio == "grupo":
        datos = {"@context": "https://schema.org", "@type": "ProductGroup", "hasVariant": [producto]}
    else:
        datos = {"@context": "https://schema.org/", "@type": "BuyAction", "object": producto}
    return ('<script type="application/ld+json">{"@type":"OnlineStore","name":"x"}</script>'
            '<script data-rh="true" type="application/ld+json">%s</script>' % json.dumps(datos))


class Tiendas(unittest.TestCase):
    def tienda(self, nombre="Powerplanet", url="https://www.powerplanetonline.com/es/xiaomi-vacuum-cleaner-g20-blanco"):
        return config._tienda({"tienda": nombre, "url": url})

    def test_jsonld_powerplanet_y_mediamarkt(self):
        o = tiendas.analizar_jsonld(ficha_jsonld(), self.tienda(), EAN_G20)
        self.assertEqual((o.precio_cent, o.disponible, o.vendedor), (15995, True, "Powerplanet"))
        self.assertEqual(o.imagen, "https://img.example/g20.jpg")
        mm = self.tienda("MediaMarkt", "https://www.mediamarkt.es/es/product/_g20-148640073.html")
        o = tiendas.analizar_jsonld(ficha_jsonld(180.27, vendedor="Tek4life-ES", envoltorio="buy"), mm, EAN_G20)
        self.assertEqual((o.precio_cent, o.vendedor, o.clave), (18027, "Tek4life-ES", "mediamarkt:_g20-148640073"))

    def test_jsonld_otra_variante_no_cuela(self):
        with self.assertRaises(tiendas.ErrorTienda):
            tiendas.analizar_jsonld(ficha_jsonld(ean="6978515257568"), self.tienda(), "6978515257575")

    def test_jsonld_agotado_y_segunda_mano(self):
        o = tiendas.analizar_jsonld(ficha_jsonld(disponibilidad="OutOfStock"), self.tienda(), EAN_G20)
        self.assertEqual((o.disponible, o.precio_cent), (False, None))
        with self.assertRaises(tiendas.ErrorTienda, msg="el precio de 2a mano no es el de nuevo"):
            tiendas.analizar_jsonld(ficha_jsonld(condicion="UsedCondition"), self.tienda(), EAN_G20)
        with self.assertRaises(tiendas.ErrorTienda):
            tiendas.analizar_jsonld("<html>sin datos</html>", self.tienda(), EAN_G20)

    def test_shopify_por_ean(self):
        t = config._tienda({"tienda": "Dreame", "url": "https://es.dreametech.com/products/aspirador-sin-cable-u20"})
        self.assertEqual((t.lector, t.clave), ("shopify", "dreame:aspirador-sin-cable-u20"))
        texto = json.dumps({"title": "Dreame U20", "featured_image": "//cdn.shopify.com/u20.jpg", "variants": [
            {"id": 1, "barcode": "111", "price": 5000, "available": True},
            {"id": 2, "barcode": "6976233670607", "price": 19900, "available": False}]})
        o = tiendas.analizar_shopify(texto, t, "6976233670607")
        self.assertEqual((o.disponible, o.precio_cent, o.imagen), (False, None, "https://cdn.shopify.com/u20.jpg"))
        texto = texto.replace('"available": false', '"available": true')
        self.assertEqual(tiendas.analizar_shopify(texto, t, "6976233670607").precio_cent, 19900)

    def test_desafio_anti_bots_no_es_un_precio(self):
        self.assertTrue(tiendas.DESAFIO.search("<title>Just a moment...</title>"))
        self.assertTrue(tiendas.DESAFIO.search('<script src="/cdn-cgi/challenge-platform/h/b">'))

    def test_pccomponentes_solo_como_enlace(self):
        with self.assertRaises(ValueError):
            config._tienda({"tienda": "PcComponentes", "url": "https://www.pccomponentes.com/dreame-u20"})
        cfg = config.ejemplo()
        cfg["productos"][1]["tiendas"].append({"tienda": "PcComponentes", "url": "https://www.pccomponentes.com/x"})
        with self.assertRaises(config.ErrorConfig):
            config.validar(cfg)


class VariasTiendas(unittest.TestCase):
    """La G20 en Amazon, Powerplanet, MediaMarkt y PcComponentes: cuenta el mejor precio con stock."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["HISTORY_FILE"] = str(self.tmp / "historico_precios.csv")
        self.a = Almacen(self.tmp / "precios.db")
        self.cfg = configuracion(None)
        self.cfg["productos"] = [config.ejemplo()["productos"][0]]  # la G20 de ejemplo, 4 tiendas
        self.canal = notificadores.Consola(salida=lambda *_: None)
        self.precios = {"Amazon": None, "Powerplanet": 15995, "MediaMarkt": 18027, "PcComponentes": 17699}

    def lector(self, t, p):
        precio = self.precios[t.nombre]
        if isinstance(precio, Exception):
            raise precio
        return Observacion(t.clave, T0, t.lector, disponible=precio is not None, precio_cent=precio,
                           vendedor={"MediaMarkt": "Tek4life-ES"}.get(t.nombre, t.nombre))

    def vuelta(self, horas):
        return seguimiento.ciclo(self.cfg, self.a, self.canal, ahora=T0 + timedelta(hours=horas),
                                 lector=self.lector, dormir=lambda s: None)

    def test_primera_vez_no_avisa_de_una_reposicion_falsa(self):
        est = self.a.estado(ASIN)  # venia agotada en Amazon (formato antiguo)
        est["disponible"] = False
        self.a.guardar_estado(ASIN, est)
        self.vuelta(0)
        self.assertEqual(self.canal.enviadas, [], "Powerplanet siempre tuvo stock: no es una reposicion")
        est_p = self.a.estado("producto:" + ASIN)
        self.assertEqual((est_p["mejor_precio_cent"], est_p["mejor_tienda"]), (15995, "Powerplanet"))

    def test_bajada_en_otra_tienda_con_comparacion_y_enlaces(self):
        self.vuelta(0)
        self.precios["MediaMarkt"] = 14000
        self.vuelta(2)
        self.assertEqual(len(self.canal.enviadas), 1)
        t = self.canal.enviadas[0]
        self.assertEqual((t.tipo, t.precio_cent, t.boton), ("bajada", 14000, "🛒 Ver en MediaMarkt"))
        self.assertIn("mediamarkt.es", t.url)
        self.assertEqual(t.referencia_cent, 15995, "antes: el mejor precio anterior, de otra tienda")
        self.assertEqual((t.tienda, t.vendedor), ("MediaMarkt", "Tek4life-ES"))
        self.assertEqual(t.otras, "🏬 Amazon sin stock · Powerplanet 159,95 € · PcComponentes 176,99 €")
        self.assertIn("💶 140,00 € en MediaMarkt (vende Tek4life-ES) · antes ~159,95 €~ (−19,95 € · −12 %)",
                      notificadores.a_texto(t) if hasattr(notificadores, "a_texto") else
                      __import__("vigilante.tarjeta", fromlist=["a_texto"]).a_texto(t))
        self.assertEqual([e[0] for e in t.enlaces], ["Xiaomi"])
        botones = boton_telegram(t)["inline_keyboard"]
        self.assertEqual(botones[0][0]["text"], "🛒 Ver en MediaMarkt")
        self.assertEqual([b["text"] for b in botones[1]], ["🔗 Xiaomi"])
        self.assertIn("[Xiaomi](https://www.mi.com/", a_discord(t)["embeds"][0]["description"])

    def test_leer_una_tienda_mas_cara_no_es_una_bajada(self):
        self.vuelta(0)
        self.precios["Powerplanet"] = 15995
        self.vuelta(2)
        self.vuelta(4)
        self.assertEqual(self.canal.enviadas, [])

    def test_lecturas_antiguas_no_cuentan(self):
        self.vuelta(0)
        self.precios["Powerplanet"] = tiendas.BloqueoTienda("HTTP 429")
        for h in (2, 4, 6, 8, 10, 12):
            self.vuelta(h)
        sit = {s["tienda"].nombre: s for s in seguimiento.situacion(
            config.productos(self.cfg)[0], self.a, self.cfg["seguimiento"], T0 + timedelta(hours=12))}
        self.assertFalse(sit["Powerplanet"]["reciente"], "una lectura de hace 12 h ya no vale")
        self.assertEqual(self.a.estado("producto:" + ASIN)["mejor_tienda"], "PcComponentes")
        self.assertEqual(self.a.serie("powerplanet:xiaomi-vacuum-cleaner-g20-blanco")[-1][1], 15995,
                         "el fallo no pisa el ultimo precio bueno")

    def test_cada_tienda_avisa_de_sus_cambios_aunque_no_sea_la_mejor(self):
        self.vuelta(0)
        self.vuelta(1)
        self.assertEqual(self.canal.enviadas, [], "sin cambios, nada")
        self.precios["MediaMarkt"] = 18026  # un centimo, y no es la mas barata
        self.vuelta(2)
        t = self.canal.enviadas[-1]
        self.assertEqual((t.tipo, t.precio_cent, t.boton), ("cambio_tienda", 18026, "🛒 Ver en MediaMarkt"))
        self.assertEqual((t.referencia_cent, t.notas), (18027, []))
        self.assertEqual(t.otras, "🏆 Lo más barato ahora: 159,95 € en Powerplanet")
        self.vuelta(3)
        self.assertEqual(len(self.canal.enviadas), 1, "el mismo precio no se repite")

    def test_cambios_simultaneos_en_una_sola_tarjeta(self):
        self.vuelta(0)
        self.precios.update(Powerplanet=15994, MediaMarkt=None, PcComponentes=17999)
        self.vuelta(2)
        self.assertEqual(len(self.canal.enviadas), 1)
        t = self.canal.enviadas[0]
        self.assertEqual((t.tipo, t.precio_cent), ("bajada", 15994))
        self.assertEqual(t.notas, ["❌ MediaMarkt: se queda sin stock (estaba a 180,27 €)",
                                   "📈 PcComponentes: 176,99 € → 179,99 € (+3,00 €)"],
                         "el de Powerplanet ya es la linea del precio")

    def test_anadir_una_tienda_mas_barata_no_es_una_bajada(self):
        del self.cfg["productos"][0]["tiendas"][3]  # sin PcComponentes
        self.vuelta(0)
        self.cfg["productos"] = [config.ejemplo()["productos"][0]]
        self.precios["PcComponentes"] = 13000  # por encima del objetivo de 125
        self.vuelta(1)
        self.assertEqual([t.tipo for t in self.canal.enviadas], [], "ha entrado una tienda, no ha bajado nada")
        self.assertEqual(self.a.estado("producto:" + ASIN)["mejor_precio_cent"], 13000)
        self.precios["PcComponentes"] = 12999
        self.vuelta(2)
        self.assertEqual([t.tipo for t in self.canal.enviadas], ["bajada"])

    def test_un_fallo_no_es_un_cambio_de_stock(self):
        self.vuelta(0)
        self.precios["MediaMarkt"] = tiendas.BloqueoTienda("HTTP 429")
        self.vuelta(1)
        self.assertEqual(self.canal.enviadas, [])

    def test_reposicion_en_cualquier_tienda_y_objetivo(self):
        self.precios.update(Powerplanet=None, MediaMarkt=None, PcComponentes=None)
        self.vuelta(0)
        self.precios["MediaMarkt"] = 12400
        self.vuelta(2)
        t = self.canal.enviadas[-1]
        self.assertEqual(t.tipo, "objetivo")
        self.assertIn("📦 Vuelve a haber stock", t.notas)
        self.assertEqual(len(t.notas), 1, "sin repetir lo mismo")


PCCOM_G20 = "https://www.pccomponentes.com/xiaomi-vacuum-cleaner-g20-aspiradora-escoba-mano-sin-cable-460w-beige"


def pagina_pccomtracker(url=PCCOM_G20, precio="176.99", stock="InStock", modificado="2026-11-01T10:06:58.000Z"):
    """Recorte con la misma estructura que la ficha real de pccomtracker (21/11/2026)."""
    prod = {"@context": "https://schema.org", "@type": "Product",
            "name": "Xiaomi Vacuum Cleaner G20 Aspiradora Escoba/Mano sin Cable 460W Beige",
            "image": "https://thumb.pccomponentes.com/w-530-530/g20.jpg", "dateModified": modificado,
            "offers": {"@type": "AggregateOffer", "lowPrice": precio, "highPrice": precio, "priceCurrency": "EUR",
                       "offerCount": 1, "offers": [{"@type": "Offer", "name": "PcComponentes", "price": precio,
                                                    "priceCurrency": "EUR",
                                                    "availability": "https://schema.org/" + stock, "url": url}]}}
    faq = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": "¿Cuál es el precio más bajo del último año de Xiaomi G20?",
         "acceptedAnswer": {"@type": "Answer", "text": "El precio más bajo registrado de Xiaomi G20 ha sido "
                                                       "169,99\u00a0€, alcanzado el 2 de noviembre de 2026."}},
        {"@type": "Question", "name": "¿Es buen momento para comprar Xiaomi G20?",
         "acceptedAnswer": {"@type": "Answer", "text": "Sí, el precio actual (176,99\u00a0€) está por debajo "
                                                       "de su precio habitual (179,00\u00a0€)."}}]}
    return "".join('<script type="application/ld+json">%s</script>' % json.dumps(d, ensure_ascii=False)
                   for d in (prod, faq))


REACO_G20 = ('<script>document.addEventListener("DOMContentLoaded", function() {\n const rawPoints = '
             '[{"fecha":"2026-10-08","precio":214.99},{"fecha":"2026-11-07","precio":169.99},'
             '{"fecha":"2026-11-11","precio":179},{"fecha":"2026-11-20","precio":176.99},'
             '{"fecha":"2026-11-21","precio":176.99}];\n const labels = rawPoints.map(p => {</script>')


class Terceros(unittest.TestCase):
    """PcComponentes via pccomtracker e historicos de reacofertas (formato real del 21/11/2026)."""

    def tienda(self):
        return config._tienda({"tienda": "PcComponentes", "url": PCCOM_G20,
                               "pccomtracker": "https://pccomtracker.com/producto/g20-71159"})

    def test_config_pccomponentes(self):
        t = self.tienda()
        self.assertEqual((t.lector, t.clave), ("pccomtracker", "pccomponentes:" + PCCOM_G20.rsplit("/", 1)[1]))
        with self.assertRaises(ValueError):
            config._tienda({"tienda": "PcComponentes", "url": PCCOM_G20})  # sin pccomtracker no se lee
        with self.assertRaises(ValueError):
            config._tienda({"tienda": "PcComponentes", "url": PCCOM_G20, "pccomtracker": "https://otra.web/x"})

    def test_precio_actual(self):
        o = terceros.analizar_pccomtracker(pagina_pccomtracker(), self.tienda(), ahora=T0)
        self.assertEqual((o.precio_cent, o.disponible, o.vendedor, o.fuente), (17699, True, "PcComponentes",
                                                                               "pccomtracker"))
        agotado = terceros.analizar_pccomtracker(pagina_pccomtracker(stock="OutOfStock"), self.tienda(), ahora=T0)
        self.assertEqual((agotado.precio_cent, agotado.disponible), (None, False))

    def test_otra_ficha_o_dato_antiguo_no_es_un_precio(self):
        with self.assertRaisesRegex(terceros.ErrorTercero, "no es la de"):
            terceros.analizar_pccomtracker(pagina_pccomtracker(url=PCCOM_G20 + "-max"), self.tienda(), ahora=T0)
        with self.assertRaisesRegex(terceros.ErrorTercero, "no se usa"):
            terceros.analizar_pccomtracker(pagina_pccomtracker(), self.tienda(), ahora=T0 + timedelta(days=2))

    def test_resumen_del_faq(self):
        r = terceros.resumen_pccomtracker(pagina_pccomtracker())
        self.assertEqual((r["minimo_cent"], r["minimo_fecha"].date().isoformat(), r["habitual_cent"]),
                         (16999, "2026-11-02", 17900))

    def test_serie_reacofertas(self):
        puntos = terceros.serie_reacofertas(REACO_G20)
        self.assertEqual(len(puntos), 5)
        self.assertEqual(min(p for _, p in puntos), 16999)
        self.assertTrue(terceros.cuadra(puntos, 17699))
        self.assertFalse(terceros.cuadra(puntos, 12400), "si no cuadra con la tienda, no es su serie")
        with self.assertRaises(terceros.ErrorTercero):
            terceros.serie_reacofertas("<html>Tracking y Evolución de Precios</html>")
        self.assertTrue(terceros.cubre_reacofertas("https://www.powerplanetonline.com/es/x"))
        self.assertFalse(terceros.cubre_reacofertas("https://es.dreametech.com/products/x"))

    def test_ciclo_guarda_historicos_y_la_tarjeta_los_cita(self):
        tmp = Path(tempfile.mkdtemp())
        os.environ["HISTORY_FILE"] = str(tmp / "h.csv")
        a = Almacen(tmp / "p.db")
        cfg = configuracion(None)
        g20 = config.ejemplo()["productos"][0]
        g20["tiendas"] = [t for t in g20["tiendas"] if t["tienda"] == "PcComponentes"]
        cfg["productos"] = [g20]
        canal = notificadores.Consola(salida=lambda *_: None)
        precio = {"v": 17699}
        pedidas = []

        def descargar(url):
            pedidas.append(url)
            if "reacofertas" in url:
                return REACO_G20
            return pagina_pccomtracker(precio="%.2f" % (precio["v"] / 100), modificado=iso(T0))

        def lector(t, p):
            return terceros.analizar_pccomtracker(descargar(t.extra["pccomtracker"]), t, ahora=T0)

        seguimiento.ciclo(cfg, a, canal, ahora=T0, lector=lector, descargar=descargar)
        clave = config.productos(cfg)[0].tiendas[0].clave
        self.assertEqual(a.historial_externo(clave, "reacofertas")[1][1][1], 16999)
        self.assertEqual(a.historial_externo(clave, "pccomtracker")[1], [(datetime(2026, 11, 2, tzinfo=timezone.utc),
                                                                          16999)])
        n = len(pedidas)
        precio["v"] = 16500
        seguimiento.ciclo(cfg, a, canal, ahora=T0 + timedelta(hours=1), lector=lector, descargar=descargar)
        self.assertEqual(len(pedidas), n + 1, "los historicos se refrescan cada 12 h, no en cada lectura")
        t = canal.enviadas[-1]
        self.assertEqual((t.tipo, t.precio_cent, t.boton), ("bajada", 16500, "🛒 Ver en PcComponentes"))
        self.assertIn("pccomtracker", t.fuente)
        self.assertTrue(t.veredicto.startswith("🏆 Nuevo mínimo de 6 meses"), t.veredicto)
        self.assertIn("mínimo: reacofertas", t.fuente)

    def test_veredicto_en_una_linea(self):
        a = Almacen(":memory:")
        p = config.productos({"productos": [config.ejemplo()["productos"][0]]})[0]
        t = p.tiendas[3]
        ahora = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
        a.importar_historial(t.clave, "reacofertas", ahora, [(datetime(2026, 10, 8, tzinfo=timezone.utc), 21499),
                                                             (datetime(2026, 11, 7, tzinfo=timezone.utc), 16999),
                                                             (datetime(2026, 11, 21, tzinfo=timezone.utc), 16500)])
        v = lambda precio: seguimiento.veredicto(p, t, precio, [], a, ahora, reglas.Motivo("bajada"), 14)
        self.assertEqual(v(16500), ("🏆 Nuevo mínimo de 6 meses en PcComponentes (antes 169,99 € el 07/11)",
                                    "reacofertas"), "el punto de hoy es el precio actual: no cuenta")
        self.assertEqual(v(16999)[0], "🏆 Iguala su mínimo de 6 meses en PcComponentes (07/11)")
        self.assertEqual(v(17699)[0], "📊 Su mínimo de 6 meses en PcComponentes: 169,99 € (07/11)")
        a.importar_historial(t.clave, "pccomtracker", ahora, [(datetime(2026, 11, 2, tzinfo=timezone.utc), 16999)])
        self.assertEqual(v(17699), ("📊 Su mínimo del año en PcComponentes: 169,99 € (02/11)", "pccomtracker"),
                         "el periodo mas largo manda")
        self.assertEqual(seguimiento.veredicto(p, p.tiendas[1], 15995, [], a, ahora, reglas.Motivo("bajada"), 14),
                         (None, None), "sin historia suficiente no se dice nada")


def chollo(titulo, precio, antes=None, texto="", categoria="Electrónica", real=None):
    return fuentes.Chollo(id="1", titulo=titulo, tienda="Amazon", precio_cent=precio, url="https://x/1",
                          publicada=T0, texto=titulo + " " + texto, precio_antes_cent=antes, categoria=categoria,
                          ref_hist_cent=real, ref_texto="precio habitual de prueba")


class ChollosComunidad(unittest.TestCase):
    """Casos reales del 21/11/2026 y los que si deben avisar."""

    def ev(self, c, exigir=False):
        """Mecanica de los niveles. Por defecto sin exigir historia real, que se
        prueba aparte (test_chollometro_sin_historia_real_no_avisa)."""
        des = config.ejemplo()["descubrimiento"]
        des["comunidad"]["exigir_historico"] = exigir
        return descubrimiento.evaluar_chollo(c, des, T0, timedelta(hours=6))

    def test_chollometro_sin_historia_real_no_avisa(self):
        for c in (chollo("ERROR DE PRECIO: Smart TV 55 pulgadas", 4900),
                  chollo("Auriculares Sony WH-1000XM5", 12900, antes=39900),
                  chollo("Dreame H12 Aspiradora sin cable", 13000, categoria="Hogar, vivienda y oficina")):
            self.assertIsNone(self.ev(c, exigir=True), c.titulo)
        self.assertEqual(self.ev(chollo("Auriculares Sony WH-1000XM5", 12900, real=39900), exigir=True), "chollo")

    def test_peines_de_3_euros_no_son_un_error_de_precio(self):
        c = chollo("Kit 3 mini cepillos pelo Beter", 395,
                   texto="A 3,95 tres unidades me parece un preciazo")
        self.assertIsNone(self.ev(c))

    def test_polo_barato_con_79_por_ciento_no_avisa(self):
        self.assertIsNone(self.ev(chollo("Polo de manga corta French Toast", 309, antes=1490)))

    def test_solo_tus_temas(self):
        self.assertIsNone(self.ev(chollo("Muñeca", 15000, antes=40000, categoria="Juguetes, familia e hijos")),
                          "62 %: un chollazo, pero no de tus temas")
        self.assertEqual(self.ev(chollo("Robot aspirador", 24900, real=69900, categoria="Hogar, vivienda y oficina")),
                         "chollo")

    def test_errores_de_precio_en_cualquier_categoria(self):
        self.assertEqual(self.ev(chollo("ERROR DE PRECIO: abrigo de marca", 4900, categoria="Moda y accesorios")),
                         "error_precio")
        self.assertEqual(self.ev(chollo("Muñeca", 5000, real=40000, categoria="Juguetes, familia e hijos")),
                         "error_precio", "88 % sobre 400 EUR, aunque no sea de tus temas")
        self.assertIsNone(self.ev(chollo("Muñeca", 5000, antes=40000, categoria="Juguetes, familia e hijos")),
                          "el 'antes' de Chollometro no cuenta ni para una alerta provisional")
        self.assertEqual(self.ev(chollo("Robot aspirador Roborock", 9900, real=69900)), "error_precio")
        self.assertIsNone(self.ev(chollo("Calcetines", 500, antes=4000)), "88 %, pero sobre 40 EUR: calderilla")

    def test_reacondicionado_nunca(self):
        self.assertIsNone(self.ev(chollo("iPhone 15 reacondicionado ERROR DE PRECIO", 9900, antes=79900)))
        self.assertIsNone(self.ev(chollo("Portátil Lenovo (Renewed)", 19900, antes=89900)))

    def test_intereses_activos(self):
        self.assertEqual(self.ev(chollo("Dreame H12 Aspiradora sin cable", 13000, categoria="Hogar, vivienda y oficina")),
                         "interes", "por debajo de tu objetivo de 140 EUR, aunque no traiga precio anterior")
        self.assertEqual(self.ev(chollo("Rowenta X-Force aspiradora escoba", 20000, antes=24000, categoria="Hogar, vivienda y oficina")),
                         "interes", "17 % y 40 EUR de ahorro")
        self.assertIsNone(self.ev(chollo("Rowenta X-Force aspiradora escoba", 23000, antes=24000)), "solo un 4 %")
        self.assertIsNone(self.ev(chollo("Bolsas para aspiradora Rowenta", 1500)), "accesorios fuera")
        self.assertIsNone(self.ev(chollo("Robot aspirador Conga", 12000)), "los robots, excluidos en tu lista")
        self.assertIsNone(self.ev(chollo("Aspiradora Dyson V15", 49900, antes=69900)), "por encima de 250 EUR")
        self.assertIsNone(self.ev(chollo("Aspirador de mano", 3000)), "por debajo de 50 EUR")

    def test_error_de_precio_en_el_titulo(self):
        self.assertEqual(self.ev(chollo("ERROR DE PRECIO: Smart TV 55 pulgadas", 4900)), "error_precio")
        self.assertIsNone(self.ev(chollo("Error de precio en calcetines", 299)), "calderilla")
        self.assertIsNone(self.ev(chollo("Cafetera", 4900, texto="quiza sea un error de precio")),
                          "en la descripcion no cuenta")

    def test_chollazo_de_verdad(self):
        self.assertEqual(self.ev(chollo("Auriculares Sony WH-1000XM5", 12900, real=39900)), "chollo")
        self.assertEqual(self.ev(chollo("Auriculares Sony WH-1000XM5", 17900, real=39900)), "extraordinaria",
                         "55 % y 220 EUR: no llega a chollazo, pero es una oferta extraordinaria")
        self.assertIsNone(self.ev(chollo("Auriculares Sony WH-1000XM5", 31900, real=39900)), "20 %: nada")

    def test_un_tachado_o_pvpr_no_basta_para_un_chollazo(self):
        self.assertIsNone(self.ev(chollo("Auriculares Sony WH-1000XM5", 12900, antes=39900)),
                          "68 % frente al 'antes' de la publicacion: sin historico no es un chollazo")
        self.assertEqual(self.ev(chollo("Auriculares Sony WH-1000XM5", 12900, antes=39900, real=21900)),
                         "extraordinaria", "frente a su habitual real es un 41 % y 90 EUR, no el 68 % anunciado")

    def test_dato_roto_no_es_un_error_de_precio(self):
        c = chollo("Corsair Hydro X Rotary Pack 2 Adaptadores", 11664, real=1209879, categoria=None)
        c.fuente = "reacofertas"
        self.assertIsNone(self.ev(c), "un 'habitual' de 12.098 EUR en un adaptador es un dato roto")

    def test_tus_productos_en_chollometro_sin_confundir_modelos(self):
        prods = config.ejemplo()["productos"]
        en = lambda t: (descubrimiento.producto_en_chollo(chollo(t, 9900), prods) or {}).get("nombre")
        self.assertEqual(en("Dreame U20 aspiradora sin cable por 99 € en PcComponentes"), "Dreame U20")
        self.assertEqual(en("Xiaomi Vacuum Cleaner G20 por 119€"), "Xiaomi G20")
        self.assertIsNone(en("Xiaomi G20 Lite por 59€"))
        self.assertIsNone(en("Xiaomi G20 Max por 189€"))
        self.assertEqual(en("Dreame R20 SE-A a 129€"), "Dreame R20 SE-A")

    def test_tarjeta_de_error(self):
        t = descubrimiento.tarjeta_chollo(chollo("ERROR DE PRECIO: TV", 4900), "error_precio",
                                          T0 + timedelta(minutes=7), fuentes.CHOLLOMETRO_ATRIBUCION)
        self.assertEqual((t.tipo, t.boton), ("error_precio", "🔥 Ver el chollo"))
        self.assertIn("hace 7 min", t.fuente)
        self.assertIn("🚨", a_telegram(t))


if __name__ == "__main__":
    unittest.main()


DATOS = Path(__file__).parent / "datos"


def dato(nombre):
    return (DATOS / nombre).read_text(encoding="utf-8")


class FuentesConHistorico(unittest.TestCase):
    """pccomtracker y reacofertas con recortes de sus paginas reales (21/11/2026)."""

    AHORA = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)

    def test_portada_de_pccomtracker(self):
        t = fuentes_historico.tarjetas_pccomtracker(dato("pccomtracker_portada.html"))
        self.assertEqual(len(t), 3)
        self.assertEqual((t[0]["precio"], t[0]["antes"]), (31008, 62484))
        self.assertTrue(t[0]["href"].startswith("/producto/cocina-independiente-beko"))

    def test_ficha_de_pccomtracker(self):
        f = fuentes_historico.ficha_pccomtracker(dato("pccomtracker_ficha_g20.html"))
        self.assertEqual((f["precio"], f["habitual"], f["minimo"], f["disponible"]), (17699, 17900, 16999, True))
        self.assertTrue(f["url"].startswith("https://www.pccomponentes.com/xiaomi-vacuum-cleaner-g20"))

    def test_reacofertas_lista_y_ficha(self):
        t = fuentes_historico.tarjetas_reacofertas(dato("reacofertas_bajadas.html"))
        self.assertEqual((len(t), t[0]["id"], t[0]["precio"], t[0]["antes"]), (6, "145431", 11664, 1209879))
        f = fuentes_historico.ficha_reacofertas(dato("reacofertas_ficha_g20.html"))
        self.assertEqual((f["tienda"], f["precio"], len(f["puntos"])), ("PcComponentes", 17699, 5))

    def test_mediana_diaria(self):
        d = lambda m, dia: datetime(2026, m, dia, tzinfo=timezone.utc)
        puntos = [(d(10, 8), 21499), (d(11, 7), 16999), (d(11, 11), 17900), (d(11, 20), 17699)]
        self.assertEqual(fuentes_historico.mediana_diaria(puntos, self.AHORA), (21499, 44),
                         "30 de los 44 dias estuvo a 214,99: un pico de un dia no la mueve")
        self.assertEqual(fuentes_historico.mediana_diaria([(d(11, 20), 100)], self.AHORA), (100, 1))

    def _fuente(self, clase, paginas):
        cfg = config.ejemplo()
        a = Almacen(":memory:")
        pedidas = []

        def descargar(url):
            pedidas.append(url)
            for clave, nombre in paginas.items():
                if clave in url:
                    return dato(nombre)
            raise terceros.ErrorTercero("404")
        return clase(cfg, a, descargar, ahora=self.AHORA), pedidas, cfg

    def test_pccomtracker_con_referencia_real_y_cache(self):
        f, pedidas, cfg = self._fuente(fuentes_historico.PCComTracker, {
            "/producto/": "pccomtracker_ficha_g20.html", "/categoria/": "pccomtracker_portada.html",
            "pccomtracker.com/": "pccomtracker_portada.html"})
        lote = f.candidatos()
        self.assertTrue(lote)
        c = lote[0]
        self.assertEqual((c.fuente, c.tienda, c.ref_hist_cent, c.minimo_cent), ("pccomtracker", "PcComponentes",
                                                                               17900, 16999))
        self.assertTrue(c.url.startswith("https://www.pccomponentes.com/"), "el boton va a la tienda, sin afiliado")
        n = len(pedidas)
        f.candidatos()
        self.assertEqual(len(pedidas) - n, 1 + len(cfg["descubrimiento"]["intereses"][0]["pccomtracker_categorias"]),
                         "las fichas se recuerdan 6 h: solo se piden las listas")

    def test_reacofertas_sin_afiliados_y_con_mediana(self):
        f, _, _ = self._fuente(fuentes_historico.Reacofertas, {
            "?id=": "reacofertas_ficha_g20.html", "tracking-precios": "reacofertas_bajadas.html"})
        c = f.candidatos()[0]
        self.assertEqual((c.ref_hist_cent, c.ref_texto), (21499, "mediana de 90 días según reacofertas"))
        self.assertTrue(c.url.startswith("https://reacofertas.es/tracking-precios?id="))
        self.assertEqual((c.minimo_cent, c.minimo_fecha.day), (16999, 7))

    def test_catalogo_sin_referencia_real_no_es_interes(self):
        des = config.ejemplo()["descubrimiento"]
        c = fuentes.Chollo(id="9", titulo="Aspiradora escoba Adler AD 7036", tienda="PcComponentes", precio_cent=6020,
                           url="https://x", publicada=self.AHORA, texto="", fuente="reacofertas", es_oferta=False,
                           precio_antes_cent=7105, minimo_cent=5208)
        self.assertIsNone(descubrimiento.evaluar_chollo(c, des, self.AHORA, timedelta(hours=48)),
                          "15 % frente al precio anterior, pero sin historico real: hace una semana estaba a 52 EUR")
        c.ref_hist_cent = 9341
        self.assertEqual(descubrimiento.evaluar_chollo(c, des, self.AHORA, timedelta(hours=48)), "interes")

    def test_datos_rotos_de_reacofertas_no_avisan(self):
        des = config.ejemplo()["descubrimiento"]
        for x in fuentes_historico.tarjetas_reacofertas(dato("reacofertas_bajadas.html")):
            c = fuentes.Chollo(id=x["id"], titulo=x["titulo"], tienda="PcComponentes", precio_cent=x["precio"],
                               url="https://x", publicada=self.AHORA, texto=x["titulo"], fuente="reacofertas",
                               precio_antes_cent=x["antes"], ref_hist_cent=x["antes"])
            self.assertIsNone(descubrimiento.evaluar_chollo(c, des, self.AHORA, timedelta(hours=48)), x["titulo"])


class JuezChollometro(unittest.TestCase):
    """Chollometro detecta, la historia real juzga. Caso real: el Newskill
    Gungnyr TKL Pro Ivory a 33,99 EUR en PcComponentes (24/11/2026), con
    precio habitual de 130,99 EUR en pccomtracker."""

    AHORA = datetime(2026, 11, 24, 7, 0, tzinfo=timezone.utc)
    TITULO = "Newskill Gungnyr TKL Pro Ivory Teclado Optomecánico Gaming RGB por 33,99€"
    SLUGS = ["newskill-gungnyr-tkl-pro-ivory-teclado-optomecanico-gaming-rgb-switches-gateron-red-intercambiables-19101",
             "newskill-gungnyr-tkl-pro-teclado-optomecanico-gaming-rgb-switches-gateron-red-intercambiables-negro-19100",
             "newskill-gungnyr-tkl-teclado-mecanico-gaming-rgb-switch-red-19050"]

    def test_tokens_sin_precio(self):
        self.assertEqual(juez.tokens(self.TITULO), ["newskill", "gungnyr", "tkl", "pro", "ivory", "teclado",
                                                    "optomecanico", "gaming"])

    def test_elige_la_variante_exacta(self):
        cands = [(s.replace("-", " "), s) for s in self.SLUGS]
        self.assertEqual(juez.elegir(self.TITULO, cands), self.SLUGS[0], "Ivory, no el negro ni el TKL a secas")
        self.assertIsNone(juez.elegir("Newskill Gungnyr TKL Max teclado", cands), "no existe el Max")
        self.assertEqual(juez.elegir("Newskill Gungnyr TKL teclado mecanico", cands), self.SLUGS[2],
                         "sin Pro en la publicacion, el Pro es otro modelo")
        self.assertIsNone(juez.elegir("Samsung Galaxy S24 128GB", cands))

    def _juez(self):
        cfg = config.ejemplo()
        a = Almacen(":memory:")
        pedidas = []
        sitemap = "".join("<loc>https://pccomtracker.com/producto/%s</loc>" % s for s in self.SLUGS)

        def descargar(url):
            pedidas.append(url)
            if url.endswith("/sitemap/0.xml"):
                return sitemap
            if "/sitemap/" in url:
                raise terceros.ErrorTercero("404")
            if "gungnyr-tkl-pro-ivory" in url:
                return dato("pccomtracker_ficha_teclado.html")
            raise terceros.ErrorTercero("404")
        return juez.Juez(cfg, a, descargar, dormir=lambda s: None), a, pedidas, cfg

    def test_indice_semanal(self):
        j, a, pedidas, _ = self._juez()
        self.assertEqual(j.actualizar_indice(self.AHORA), 3)
        self.assertEqual(j.actualizar_indice(self.AHORA + timedelta(days=2)), 0, "una vez por semana")
        self.assertEqual(j.buscar_pccomtracker(self.TITULO), "https://pccomtracker.com/producto/" + self.SLUGS[0])

    def test_el_teclado_es_un_chollazo_de_verdad(self):
        j, a, pedidas, cfg = self._juez()
        j.actualizar_indice(self.AHORA)
        c = fuentes.Chollo(id="2087000", titulo=self.TITULO, tienda="PcComponentes", precio_cent=3399,
                           url="https://www.chollometro.com/ofertas/newskill-gungnyr-2087000",
                           publicada=self.AHORA - timedelta(minutes=12), texto=self.TITULO,
                           categoria="Electrónica", precio_antes_cent=14495)
        canal = notificadores.Consola(salida=lambda *_: None)
        cfg["descubrimiento"]["horario"] = None
        descubrimiento._procesar_comunidad([(fuentes.Chollometro(), c, timedelta(hours=6))], cfg, a, canal,
                                           self.AHORA, self.AHORA, {}, j)
        t = canal.enviadas[0]
        self.assertEqual((t.tipo, t.precio_cent, t.referencia_cent, t.referencia_etiqueta),
                         ("chollo", 3399, 13099, "habitual"), "−74 % frente a su precio habitual real, no al PVPR")
        self.assertIn("Nuevo mínimo del año", t.veredicto)
        self.assertEqual(t.enlaces[0][0], "📈 Historial")
        n = len(pedidas)
        descubrimiento._procesar_comunidad([(fuentes.Chollometro(), c, timedelta(hours=6))], cfg, a, canal,
                                           self.AHORA, self.AHORA, {}, j)
        self.assertEqual((len(canal.enviadas), len(pedidas)), (1, n), "ni se repite ni se vuelve a consultar")

    def test_lo_que_no_es_de_esas_tiendas_ni_se_consulta(self):
        j, a, pedidas, cfg = self._juez()
        c = fuentes.Chollo(id="1", titulo="Bombones Ferrero Rocher 24 unidades", tienda="Supercor", precio_cent=649,
                           url="https://x", publicada=self.AHORA, texto="", precio_antes_cent=2163)
        self.assertFalse(j.juzgar(c, self.AHORA))
        self.assertEqual(pedidas, [])


NIV = niveles.POR_DEFECTO


class Oportunidades(unittest.TestCase):
    """Detector de chollos excepcionales: casos obligatorios."""

    AHORA = datetime(2026, 11, 24, 6, 58, tzinfo=timezone.utc)  # 07:58 en Madrid

    def teclado(self, fuente="pccomtracker", **kw):
        """Newskill Gungnyr TKL Pro Ivory en PcComponentes (24/11/2026): 33,99 EUR,
        minimo de 30 dias 116,99 EUR, PVPR 144,95 EUR."""
        c = fuentes.Chollo(id="10041515", titulo="Newskill Gungnyr TKL Pro Ivory Teclado Optomecánico Gaming RGB",
                           tienda="PcComponentes", precio_cent=3399,
                           url="https://www.pccomponentes.com/newskill-gungnyr-tkl-pro-ivory-teclado-optomecanico-gaming-rgb-switches-gateron-red-intercambiables",
                           publicada=self.AHORA - timedelta(minutes=5), texto="", fuente=fuente,
                           categoria="Periféricos › Teclados gaming", precio_antes_cent=14495, min30_cent=11699)
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def des(self):
        d = config.ejemplo()["descubrimiento"]
        d["horario"] = None
        return d

    # ------------------------------------------------------------ regresion
    def test_regresion_teclado_newskill(self):
        v = niveles.evaluar(niveles.Evidencia(precio=3399, min30=11699, anunciado=14495), NIV, "pccomtracker")
        self.assertEqual((v.nivel, round(v.pct, 2), v.ahorro, v.provisional), ("chollazo", 70.95, 8300, False))
        self.assertAlmostEqual(v.anunciado_pct, 76.55, places=1)
        c = self.teclado()
        self.assertEqual(descubrimiento.evaluar_chollo(c, self.des(), self.AHORA, timedelta(hours=26)), "chollo",
                         "chollazo excepcional aunque los teclados no esten en tus intereses")
        a, canal = Almacen(":memory:"), notificadores.Consola(salida=lambda *_: None)
        cfg = config.ejemplo()
        cfg["descubrimiento"]["horario"] = {"desde": "09:00", "hasta": "23:00"}  # 07:58: fuera de horario
        descubrimiento._procesar_comunidad([(fuentes_historico.PCComTracker(cfg, a), c, timedelta(hours=26))],
                                           cfg, a, canal, self.AHORA, self.AHORA, {})
        t = canal.enviadas[0]
        self.assertEqual((t.tipo, t.precio_cent, t.referencia_cent, t.referencia_etiqueta),
                         ("chollo", 3399, 11699, "mín. 30 días"), "sale a las 07:58: un chollazo no espera")
        self.assertEqual(t.notas[0], "✅ Descuento real: −70 % (−83,00 €) frente a su mínimo de 30 días")
        self.assertIn("🏷️ Anunciado: −76 % sobre 144,95 € (descuento comercial)", t.notas)
        self.assertIn("CHOLLAZO EXCEPCIONAL", a_telegram(t).upper())

    # ------------------------------------------------- PVPR inflado (real)
    def test_descuento_ficticio_sobre_pvpr_inflado(self):
        refs = amazon.referencias(dato("amz_B0H4H89QXV.html"))
        self.assertEqual(refs, {"min30": 37990, "pvpr": 54990})
        ev = niveles.Evidencia(precio=34990, min30=refs["min30"], anunciado=refs["pvpr"])
        self.assertIsNone(niveles.evaluar(ev, NIV, "amazon_ofertas"),
                          "REDMI Note 17 Pro: −36 % anunciado frente al PVPR, −8 % real frente al minimo de 30 dias")
        self.assertIsNone(descubrimiento.evaluar_chollo(self.teclado(precio_cent=9999), self.des(), self.AHORA,
                                                        timedelta(hours=26)),
                          "31 % anunciado sobre el PVPR, pero un 14 % frente al minimo de 30 dias")

    def test_precio_mediano_de_amazon(self):
        refs = amazon.referencias(dato("amz_B0G4ZCRY41.html"))
        self.assertEqual(refs, {"min30": 123405, "mediano": 118915})

    # --------------------------------------------------- errores y topes
    def test_errores_y_datos_rotos(self):
        ev = lambda precio, ref: niveles.evaluar(niveles.Evidencia(precio=precio, min30=ref), NIV)
        self.assertEqual(ev(4999, 49900).nivel, "error")
        self.assertIsNone(ev(990, 49900), "98 %: un dato roto, no un error de precio")
        self.assertEqual(ev(1500, 4900).nivel, "chollazo", "69 % sobre 49 EUR: no es error (vale menos de 50), si chollazo")

    def test_liquidaciones_y_ofertas_extraordinarias(self):
        ev = lambda precio, ref: niveles.evaluar(niveles.Evidencia(precio=precio, habitual=ref), NIV)
        self.assertEqual(ev(29900, 59900).nivel, "extraordinaria", "liquidacion: 50 % y 300 EUR")
        self.assertEqual(ev(104900, 149900).nivel, "extraordinaria", "30 % y 450 EUR en algo caro")
        self.assertIsNone(ev(8900, 12900), "31 % y 40 EUR: oferta normal")
        self.assertIsNone(ev(5900, 9900), "40 % pero solo 40 EUR")

    def test_historial_incompleto_alerta_provisional(self):
        v = niveles.evaluar(niveles.Evidencia(precio=3399, anunciado=14495), NIV, "amazon_ofertas")
        self.assertTrue(v.provisional)
        self.assertEqual(v.nivel, "chollazo")
        self.assertIn("histórico real", v.pendiente[0])
        self.assertIsNone(niveles.evaluar(niveles.Evidencia(precio=29900, anunciado=59900), NIV, "amazon_ofertas"),
                          "sin historia, una extraordinaria frente al PVPR no basta ni para provisional")
        self.assertIsNone(niveles.evaluar(niveles.Evidencia(precio=3399, anunciado=14495), NIV, "chollometro"),
                          "el 'antes' de Chollometro nunca da alertas provisionales")
        c = self.teclado(min30_cent=None)
        self.assertEqual(descubrimiento.evaluar_chollo(c, self.des(), self.AHORA, timedelta(hours=26)), "chollo")
        t = descubrimiento.tarjeta_chollo(c, "chollo", self.AHORA, "")
        self.assertTrue(t.notas[0].startswith("⏳ PROVISIONAL: −76 % frente al precio anterior"), t.notas)

    def test_fuera_de_categorias(self):
        des = self.des()
        des["comunidad"]["categorias"] = ["Gaming"]
        c = self.teclado(fuente="chollometro", categoria="Moda y accesorios", ref_hist_cent=13099)
        self.assertEqual(descubrimiento.evaluar_chollo(c, des, self.AHORA, timedelta(hours=6)), "chollo")

    def test_ventanas_rapidas(self):
        des = self.des()
        ajustes = des["fuentes"]["amazon_ofertas"]
        madrugada = datetime(2026, 11, 24, 6, 30, tzinfo=timezone.utc)   # 07:30 en Madrid
        tarde = datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(descubrimiento.intervalo("amazon_ofertas", ajustes, des, madrugada), 10)
        self.assertEqual(descubrimiento.intervalo("amazon_ofertas", ajustes, des, tarde), 15)
        self.assertEqual(descubrimiento.intervalo("chollometro", des["fuentes"]["chollometro"], des, madrugada), 3)

    # ------------------------------------------- duplicados y nuevas bajadas
    def test_oferta_vieja_no_es_noticia(self):
        des = self.des()
        monitor = fuentes.Chollo(id="m", titulo="Monitor Gigabyte GS25F14 24,5 FullHD 144Hz", tienda="PcComponentes",
                                 precio_cent=7499, url="https://x", publicada=self.AHORA, texto="", fuente="pccomtracker",
                                 ref_hist_cent=14493, minimo_cent=7499,
                                 minimo_fecha=datetime(2026, 11, 16, tzinfo=timezone.utc))
        self.assertIsNone(descubrimiento.evaluar_chollo(monitor, des, self.AHORA, timedelta(hours=26)),
                          "a 74,99 EUR desde el 16/11: su precio habitual antiguo no lo convierte en chollo")
        teclado = self.teclado(min30_cent=None, ref_hist_cent=13099, minimo_cent=8199,
                               minimo_fecha=datetime(2026, 11, 9, tzinfo=timezone.utc))
        self.assertEqual(descubrimiento.evaluar_chollo(teclado, des, self.AHORA, timedelta(hours=26)), "chollo",
                         "por debajo de su minimo reciente (81,99 EUR): bajada nueva, −74 % frente al habitual")
        aspiradora = fuentes.Chollo(id="a", titulo="Bosch BBHF220 Aspirador Escoba sin Bolsa 18V", tienda="PcComponentes",
                                    precio_cent=12099, url="https://x", publicada=self.AHORA, texto="",
                                    fuente="pccomtracker", es_oferta=False, ref_hist_cent=14401, minimo_cent=12099,
                                    minimo_fecha=datetime(2026, 11, 12, tzinfo=timezone.utc))
        self.assertIsNone(descubrimiento.evaluar_chollo(aspiradora, des, self.AHORA, timedelta(hours=26)),
                          "de tu lista, pero a ese precio desde el 12/11: no es noticia")

    def test_duplicados_entre_tiendas_y_agregadores(self):
        a, canal = Almacen(":memory:"), notificadores.Consola(salida=lambda *_: None)
        cfg = config.ejemplo()
        cfg["descubrimiento"]["horario"] = None
        pct = self.teclado()
        cm = self.teclado(fuente="chollometro", id="2087000", titulo="Teclado Newskill Gungnyr TKL Pro Ivory por 33,99€",
                          titulo_ref=pct.titulo, ref_hist_cent=13099)
        lote = [(fuentes_historico.PCComTracker(cfg, a), pct, timedelta(hours=26)),
                (fuentes.Chollometro(), cm, timedelta(hours=26))]
        descubrimiento._procesar_comunidad(lote, cfg, a, canal, self.AHORA, self.AHORA, {})
        self.assertEqual(len(canal.enviadas), 1, "pccomtracker y Chollometro: una sola alerta")
        mas_barato = self.teclado(precio_cent=3199)
        descubrimiento._procesar_comunidad([(fuentes_historico.PCComTracker(cfg, a), mas_barato, timedelta(hours=26))],
                                           cfg, a, canal, self.AHORA + timedelta(minutes=30), self.AHORA, {})
        self.assertEqual(len(canal.enviadas), 2, "vuelve a bajar un 6 %: nueva alerta")
        casi = self.teclado(precio_cent=3149)
        descubrimiento._procesar_comunidad([(fuentes_historico.PCComTracker(cfg, a), casi, timedelta(hours=26))],
                                           cfg, a, canal, self.AHORA + timedelta(minutes=60), self.AHORA, {})
        self.assertEqual(len(canal.enviadas), 2, "solo un 1,6 % menos: no se repite")


class AmazonDirecto(unittest.TestCase):
    """Amazon /deals + ficha: la fuente directa (recortes reales del 24/11/2026)."""

    def setUp(self):
        self.a = Almacen(":memory:")
        self.cfg = configuracion(None)
        self.cfg["descubrimiento"]["horario"] = None
        self.cfg["descubrimiento"]["prefiltro_anunciado_pct"] = 30  # el recorte trae ofertas del 31-36 %
        self.canal = notificadores.Consola(salida=lambda *_: None)

    def test_lista_de_ofertas(self):
        o = fuentes_tiendas.ofertas_amazon(dato("amazon_deals.html"))
        self.assertEqual(len(o), 6)
        redmi = next(x for x in o if x["asin"] == "B0H4H89QXV")
        self.assertEqual((redmi["anunciado_pct"], redmi["tipo"], redmi["estado"]), (36, "BEST_DEAL", "AVAILABLE"))
        lote = fuentes_tiendas.AmazonOfertas(lambda u: dato("amazon_deals.html")).candidatos()
        self.assertTrue(all(c.precio_cent is None for c in lote), "la lista no trae precio: lo pone la ficha")

    def ejecutar(self, fichas: dict, ahora=T0):
        def leer(asin):
            f = fichas[asin]
            return Observacion(asin, ahora, "amazon_ficha", precio_cent=f.get("precio"), disponible=f.get("stock", True),
                               valoracion=4.6, num_valoraciones=500, titulo="Producto %s" % asin,
                               min30_cent=f.get("min30"), mediano_cent=f.get("mediano"), pvpr_cent=f.get("pvpr"))
        fuente = fuentes_tiendas.AmazonOfertas(lambda u: dato("amazon_deals.html"))
        return descubrimiento.ejecutar(self.cfg, self.a, self.canal, [fuente], ahora, leer=leer,
                                       dormir=lambda s: None)

    def test_pvpr_inflado_no_avisa_y_un_chollo_real_si(self):
        fichas = {"B0H4H89QXV": {"precio": 34990, "min30": 37990, "pvpr": 54990},   # REDMI: −8 % real
                  "B0CFPWLGF2": {"precio": 49900, "min30": 129900, "pvpr": 159900},  # Kindle Paperwhite: −62 % real (precios de prueba)
                  "B07M98CL6Y": {"precio": 4999, "min30": 5299}}
        self.ejecutar(fichas)
        self.assertEqual([(t.tipo, t.precio_cent) for t in self.canal.enviadas], [("chollo", 49900)])
        motivos = [json.loads(f["datos"])["motivo"] for f in
                   self.a.db.execute("SELECT datos FROM candidatos WHERE notificado=0")]
        self.assertIn("descuento real insuficiente", motivos)

    def test_agotado_no_avisa(self):
        self.ejecutar({"B0CFPWLGF2": {"precio": None, "stock": False}, "B0H4H89QXV": {"precio": 34990, "min30": 37990},
                       "B07M98CL6Y": {"precio": 4999}})
        self.assertEqual(self.canal.enviadas, [])

    def test_caida_anomala_sin_descuento_anunciado(self):
        k = historia.clave("Amazon", asin="B0CFPWLGF2")
        for d in (12, 8, 4):
            historia.registrar(self.a, k, "amazon_ficha", "Amazon", 99900, T0 - timedelta(days=d), disponible=1)
        self.ejecutar({"B0CFPWLGF2": {"precio": 44900}, "B0H4H89QXV": {"precio": 34990, "min30": 37990},
                       "B07M98CL6Y": {"precio": 4999}})
        t = self.canal.enviadas[0]
        self.assertEqual((t.tipo, t.referencia_cent), ("extraordinaria", 99900))
        self.assertIn("frente a lo que he visto en 30 días", t.notas[0])

    def test_limite_de_fichas_por_pasada(self):
        self.cfg["descubrimiento"]["max_verificaciones_ejecucion"] = 1
        leidas = []
        fichas = {"B0H4H89QXV": {"precio": 34990, "min30": 37990}, "B0CFPWLGF2": {"precio": 49900, "min30": 129900},
                  "B07M98CL6Y": {"precio": 4999, "min30": 5299}}

        def leer(asin):
            leidas.append(asin)
            f = fichas[asin]
            return Observacion(asin, T0, "amazon_ficha", precio_cent=f["precio"], min30_cent=f["min30"],
                               valoracion=4.6, num_valoraciones=500)
        fuente = fuentes_tiendas.AmazonOfertas(lambda u: dato("amazon_deals.html"))
        descubrimiento.ejecutar(self.cfg, self.a, self.canal, [fuente], T0, leer=leer, dormir=lambda s: None)
        self.assertEqual(len(leidas), 1, "aunque la ficha leida no sea un chollo, cuenta")

    def test_historial_incompleto_provisional(self):
        self.ejecutar({"B0CFPWLGF2": {"precio": 3399, "pvpr": 14495}, "B0H4H89QXV": {"precio": 34990, "min30": 37990},
                       "B07M98CL6Y": {"precio": 4999}})
        t = self.canal.enviadas[0]
        self.assertEqual(t.tipo, "chollo")
        self.assertTrue(t.notas[0].startswith("⏳ PROVISIONAL"), t.notas)
