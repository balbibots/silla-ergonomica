# Vigilante de precio — Sihoo Doro C300

Mismo tipo de bot que [yepoda-price-bot](https://github.com/balbibots/yepoda-price-bot),
pero generalizado para vigilar **varios productos de fuentes distintas** a la
vez. De momento vigila dos:

| Producto | Fuente | Precio (2026-09-27) | Aviso si baja de |
|---|---|---|---|
| Sihoo Doro C300, blanca | [Tienda oficial](https://eu.sihoo.com/es/products/sihoo-doro-c300-ergonomischer-stuhl) | 299,99 € | 299,99 € |
| Sihoo Doro C300, gris | [Amazon.es](https://www.amazon.es/SIHOO-Doro-C300-ergon%C3%B3mica-reposabrazos/dp/B0C3TNC785) | 379,99 € | 379,99 € |

El umbral de cada uno está puesto **al precio de hoy**: te avisará en cuanto
baje aunque sea un céntimo. Súbelo en `check_price.py` (variable `PRODUCTOS`)
si solo quieres que te avise a partir de una rebaja más grande.

## ⚠️ Diferencia importante entre las dos fuentes

**Sihoo (tienda oficial) es tan fiable como Yepoda.** Es una tienda Shopify y
usa el mismo endpoint JSON (`/products/xxx.js`) — no depende de la
maquetación de la página, así que es muy difícil que se rompa.

**Amazon es más frágil, y esto no tiene solución perfecta.** Amazon no tiene
ningún endpoint público de precios. El bot lee el precio de un bloque de
datos embebido en el HTML de la página — funciona hoy, pero:
- Amazon puede rediseñar la página y romper la lectura en cualquier momento.
- Amazon bloquea con más frecuencia las peticiones automatizadas que vienen
  de IPs de centros de datos (como las de GitHub Actions) que las que
  vienen de una IP residencial normal como la de tu casa.

Por eso el bot está diseñado para que **un fallo en Amazon nunca afecte a
Sihoo**: si Amazon empieza a fallar (por bloqueo o por cambio de página), el
bot te avisará del error (máximo 1 vez cada 24h) pero seguirá vigilando
Sihoo con total normalidad. Si en la práctica Amazon empieza a fallar todo
el rato, lo más sensato será quitarlo de `PRODUCTOS` y quedarnos solo con la
fuente fiable — dímelo si llega ese caso.

**Reintento automático** (añadido tras el primer bloqueo real que nos dio
GitHub Actions): antes de darse por vencido con un producto, el bot lo
intenta hasta 2 veces, esperando 5 segundos entre intento e intento. Un
bloqueo de Amazon a una petición suelta no significa que la siguiente, unos
segundos después, también vaya a fallar — así que esto reduce los avisos de
error causados por un tropiezo puntual, sin ocultar un fallo de verdad (si
fallan los 2 intentos, sigue avisando igual). Se ajusta con `REINTENTOS` y
`ESPERA_ENTRE_REINTENTOS_SEG` en el workflow.

## Cómo lee cada precio

**Sihoo**: `https://eu.sihoo.com/.../sihoo-doro-c300-ergonomischer-stuhl.js`
→ JSON con un array `variants`, cada uno con su `id`, `price` (en céntimos) y
`available`. El bot busca la variante `44187879375062` (blanca, estándar) —
si algún día quieres vigilar el negro o la versión con reposapiés en vez de
o además de la blanca, hay que cambiar/añadir ese `variant_id` (dímelo y lo
saco de la misma URL `.js`).

**Amazon**: dentro del HTML de la ficha hay un bloque de JavaScript con los
datos de la buybox: `"desktop_buybox_group_1":[{"displayPrice":"379,99
€","priceAmount":379.99,...}]`. El bot extrae ese número con una expresión
regular. Es el mismo principio que usan camelcamelcamel o Keepa, solo que a
menor escala.

## Qué te avisa (y qué no)

| Evento | ¿Avisa? |
|---|---|
| Un precio baja de su umbral | ✅ Una vez, hasta que vuelva a subir |
| Un precio cambia (sube o baja) SIN cruzar el umbral | ✅ Con `NOTIFY_ON_ANY_CHANGE` (activado por defecto) — es el modo "seguimiento general": te enteras de cualquier movimiento de precio, no solo de las rebajas |
| Sihoo se agota / vuelve a haber stock | ✅ (solo Sihoo — Amazon no lo detecta, ver arriba) |
| Ha pasado una semana sin ningún aviso | ✅ Un único mensaje con el precio actual de **ambos** productos |
| Uno de los dos falla al leer el precio | ✅ Máximo 1 vez / 24h, **solo del que falla** |
| Todo sigue exactamente igual | ❌ Silencio |

Si en algún momento quieres volver a que solo te avise de bajadas de precio "de chollo" (sin seguimiento general de cada subida y bajada), pon `NOTIFY_ON_ANY_CHANGE: "false"` en el workflow.

La primera vez que se ejecuta el bot no avisa de nada, solo guarda el punto
de partida (precio, stock, fecha del heartbeat) de cada producto — igual que
hacía el bot de Yepoda.

## Añadir un producto nuevo en el futuro

Es lo que cambia respecto al bot de Yepoda: en vez de un script por
producto, aquí basta con añadir una entrada a la lista `PRODUCTOS` en
`check_price.py`:

```python
{
    "id": "algo_unico",
    "nombre": "Nombre para los mensajes",
    "tipo": "shopify",  # o "amazon"
    "url": "https://...",
    "variant_id": "...",  # solo si tipo == "shopify"
    "umbral_eur": 123.45,
},
```

Todo lo demás (estado, avisos, heartbeat combinado) funciona automáticamente
para el producto nuevo sin tocar nada más.

## Cómo probarlo en tu ordenador

Igual que con Yepoda — mismo bot de Telegram si quieres, o uno nuevo:

```powershell
$env:TELEGRAM_BOT_TOKEN="tu_token"
$env:TELEGRAM_CHAT_ID="tu_chat_id"
python check_price.py --test
```

Y una comprobación real (sin forzar nada):

```powershell
python check_price.py
```

## Desplegarlo en GitHub Actions

1. Sube estos archivos a un repo (privado o público, como prefieras).
2. **Settings → Secrets and variables → Actions** → crea `TELEGRAM_BOT_TOKEN`
   y `TELEGRAM_CHAT_ID`.
3. Pestaña **Actions** → **Vigilar precio de la silla** → **Run workflow**
   para probarlo a mano la primera vez.

A partir de ahí, corre solo cada 6 horas.

## Estructura

```
check_price.py                      el script (solo libreria estandar)
.github/workflows/check-price.yml   la programacion cada 6 horas
state.json                          memoria entre ejecuciones (se crea solo)
```
