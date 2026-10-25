# Vigilante de precios

Mismo tipo de bot que [yepoda-price-bot](https://github.com/balbibots/yepoda-price-bot),
pero generalizado para vigilar **varios productos de fuentes distintas** a la
vez, agrupados por categorías. De momento vigila cuatro:

| Categoría | Producto | Fuente | Último precio confirmado | Aviso si baja de |
|---|---|---|---|---|
| 🪑 Sillas | Sihoo B100, blanca | [Tienda oficial](https://eu.sihoo.com/es/products/sihoo-b100-ergonomic-office-chair-with-flip-up-armrest) | 199,99 € (2026-10-23) | 199,99 € |
| 🪑 Sillas | Sihoo B100, blanca | [Amazon.es](https://www.amazon.es/SIHOO-B100-Oficina-Soporte-Adaptable/dp/B0G5WQ65P2) | 169,99 € (2026-10-23) | 169,99 € |
| 🪑 Sillas | Flexispot ErgoX Lite, beige | [Amazon.es](https://www.amazon.es/dp/B0H3ZVDKYV) | 230,49 € con cupón de 45 € (275,49 € sin cupón, 2026-10-24) | 230,49 € |
| 🧹 Aspiradoras | Xiaomi G20 | [Amazon.es](https://www.amazon.es/dp/B0DJ82JKFL) | 125,00 € (2026-10-25; precio de referencia 199,99 €) | 125,00 € |

> Hasta el 2026-10-23 el bot vigilaba por error el modelo Doro C300; se cambió
> al B100, que es el que se quería. Las lecciones de abajo se aprendieron con
> el C300 pero valen igual.

El umbral de cada uno está puesto **al precio de hoy**: te avisará en cuanto
baje aunque sea un céntimo. Súbelo en `check_price.py` (variable `PRODUCTOS`)
si solo quieres que te avise a partir de una rebaja más grande.

## ⚠️ Por qué esto ya NO corre en GitHub Actions

**Sihoo (tienda oficial) es tan fiable como Yepoda.** Es una tienda Shopify y
usa el mismo endpoint JSON (`/products/xxx.js`) — no depende de la
maquetación de la página, así que es muy difícil que se rompa.

**Amazon, en cambio, nos dio un problema real y confirmado.** Reconstruimos
el historial completo mirando los commits de `state.json`: **las 13
ejecuciones automáticas desde el despliegue fallaron el 100% de las veces**
al intentar leer el precio en Amazon, mientras que la misma petición exacta
funcionaba sin problema desde una conexión doméstica (el PC de casa y,
después, la Raspberry). Conclusión: **Amazon tiene bloqueadas de forma permanente las
IPs de GitHub Actions** para esta página — no es un bloqueo puntual ni un
cambio de diseño de la web, es la infraestructura de GitHub la que está
identificada y filtrada.

Por eso el bot vive ahora en **Docker, en una Raspberry Pi propia** (ver
más abajo): misma IP residencial de siempre, sin ese bloqueo. El workflow de
GitHub Actions se queda solo con el botón manual (`workflow_dispatch`), por
si algún día quieres lanzarlo desde ahí para probar algo puntual, pero la
vigilancia real ya no depende de GitHub.

**Reintento automático** (se queda, sigue siendo útil): antes de darse por
vencido con un producto, el bot lo intenta hasta 2 veces, esperando 5
segundos entre intento e intento — por si el fallo es un tropiezo de red
normal y corriente, no un bloqueo de IP. Se ajusta con `REINTENTOS` y
`ESPERA_ENTRE_REINTENTOS_SEG`. Si el servidor contesta **429** con una
cabecera `Retry-After`, el bot espera lo que le pide (con un tope de 120 s,
`MAX_ESPERA_RETRY_AFTER_SEG`) en vez de los 5 s genéricos, y el aviso de error
incluye el estado, el `Retry-After` y el principio del cuerpo de la
respuesta, para no tener que adivinar qué pasó.

### Lección aprendida: Sihoo y el User-Agent de Chrome (2026-10-02)

Ya en la Raspberry, **Sihoo empezó a responder 429 en todas las
comprobaciones** (`local_rate_limited`, `Retry-After: 60`) durante unas 30 h,
a pesar de hacer solo ~4 peticiones al día. Descartamos la IP (el `curl` desde
la propia Pi, con la misma IP pública que el PC, daba 200) y luego probamos
cuatro formas de pedir lo mismo desde dentro del contenedor:

| Cabeceras | Resultado |
|---|---|
| UA de Chrome + `Accept: application/json` (como hacía el bot) | **429** |
| Solo UA de Chrome | **429** |
| UA de `curl` | 200 |
| Sin UA propio (el de Python) | 200 |

Conclusión: **disfrazar el bot de navegador era lo que hacía que Shopify lo
limitara.** Mi hipótesis (no confirmada) es que esa cadena exacta de Chrome,
muy copiada en scrapers, comparte un límite saturado. Por eso el bot ahora se
identifica con un User-Agent propio y honesto
(`silla-sihoo-bot/1.0 (+URL del repo)`) para Shopify, que nadie más comparte.
Amazon sí sigue usando el UA de navegador, porque lo necesita para servir la
página normal.

### Segunda parte: Sihoo vuelve al 429 y pasa al navegador (2026-10-24/25)

Con el cambio a la B100, Sihoo volvió a responder `429 too many requests`
pidiendo su JSON directamente, una vez cada 6 h. El patrón no tenía una causa
clara:

| Desde (misma IP pública) | Cliente | Resultado |
|---|---|---|
| PC | `curl` | 200 |
| PC | Python (`urllib`) | 429 siempre |
| Raspberry | Python | 7 ciclos bien, luego 429 |
| Raspberry | `curl` | 429 |

Un primer intento fue cambiar a `curl` (porque en el PC funcionaba), y en la
Raspberry falló igual. En vez de seguir probando clientes hasta dar con uno que
la tienda no limitase, **Sihoo se lee ahora igual que Amazon: con el navegador
real**. Abre la página normal del producto, como cualquier visitante, y desde
ella pide el JSON de producto, como hace el propio JavaScript de la tienda. En
el PC donde Python daba 429 siempre, así leyó 4 de 4. `SHOPIFY_MODO=http`
vuelve a la petición directa (útil para depurar).

### Lección aprendida: Amazon y el CAPTCHA (2026-10-08)

Ya en la Raspberry, Amazon empezó a fallar de forma intermitente: de 12 ciclos
seguidos, solo 2 leyeron el precio. El mensaje de error decía "puede que esté
agotado", pero no era eso: **Amazon respondía con una página de CAPTCHA**
(HTTP 200, ~3 KB, con `validateCaptcha`), no con un error. Las cookies que
manda (`ak_bmsc`, `bm_sv`) son de un sistema anti-bots de Akamai.

Medido el mismo día desde la IP de casa (la misma que usa la Raspberry):

| Forma de pedir la ficha | Aciertos |
|---|---|
| Cliente HTTP del bot (Python `urllib`) | 1 de 4 (y 2 de 12 ciclos en la Pi) |
| HTTP + cookies de sesión + cabeceras completas de navegador | **0 de 4** |
| **Chromium real automatizado (Playwright)** | **6 de 6** |

Por eso Amazon se lee ahora con un navegador real. Detalles que importan:

- **El reintento tras un CAPTCHA espera 60 s** (`ESPERA_CAPTCHA_SEG`), no 5:
  una segunda petición inmediata es justo lo que dispara el CAPTCHA.
- **El mensaje de error dice la causa real** (CAPTCHA, o "la página no trae el
  precio" con su tamaño y título), en vez de adivinar.
- El navegador es nuevo y sin cookies en cada comprobación, y se presenta sin
  la marca `HeadlessChrome` (es el mismo Chromium, solo que sin el rótulo de
  "controlado por software").
- **Coste:** la imagen de Docker pasa a ocupar ~2 GB en disco (~1 GB de
  descarga) y cada comprobación de Amazon usa unos cientos de MB de RAM
  durante unos segundos.
- **No hay garantía:** Amazon puede endurecer su anti-bots en cualquier
  momento. Si vuelve a fallar, el log lo dirá con claridad.

**Segundo tropiezo, de despliegue (el mismo día).** La imagen oficial de
Playwright "para Python" trae los navegadores pero **no** el paquete de Python
(lo instala en un entorno temporal y lo borra al construirse). El primer
despliegue no encontraba `playwright` y el modo `auto` caía **en silencio** al
modo HTTP, así que los CAPTCHA que seguían saliendo eran los del cliente
antiguo, no los del navegador. Ahora el `Dockerfile` instala `playwright==1.63.0`
explícitamente (y comprueba en la construcción que se puede importar), fija
`AMAZON_MODO=navegador` para que un Playwright roto dé un error claro en vez de
degradarse sin avisar, y el log indica en cada comprobación con qué método se
lee Amazon (`Amazon: leyendo con navegador real (Chromium)`).

## Cómo lee cada precio

**Sihoo**: el navegador real abre la ficha
(`https://eu.sihoo.com/es/products/sihoo-b100-...?variant=48150078357718`) y,
desde ella, pide `https://eu.sihoo.com/.../sihoo-b100-ergonomic-office-chair-with-flip-up-armrest.js`
→ JSON con un array `variants`, cada uno con su `id`, `price` (en céntimos) y
`available`. El bot busca la variante `48150078357718` (blanco, estándar) —
si algún día quieres vigilar el negro o la versión con reposapiés en vez de
o además de la blanca, hay que cambiar/añadir ese `variant_id` (dímelo y lo
saco de la misma URL `.js`).

**Amazon**: se carga la ficha con un **navegador real** (Chromium, vía
Playwright) y se lee el precio del HTML ya pintado. Se prueban tres "anclas",
de más a menos fiable, y gana la primera que aparezca:

1. Un bloque de datos embebido con el precio de la buybox:
   `"desktop_buybox_group_1":[{"displayPrice":"379,99 €","priceAmount":379.99,...}]`
2. El atributo `data-csa-c-price-to-pay="336.99"` de la opción de compra "NEW"
   (lo encontró el propio usuario inspeccionando la página; solo existe una vez
   que el navegador ha ejecutado el JavaScript de Amazon, y por eso con una
   petición HTTP simple no se veía).
3. El texto visible del precio (`apex-pricetopay-value`), con formato español
   (`1.299,00€` → 1299.0).

`AMAZON_MODO=http` fuerza el modo antiguo, sin navegador (útil para depurar);
con `auto` (por defecto) se usa el navegador si Playwright está instalado.

Además, en Amazon:

- **Cupones.** Si la ficha tiene un cupón (`Aplicar cupón de 45€` o
  `del 10 %`), el precio vigilado es el **final, con el cupón descontado**,
  que es lo que pagarías. Los avisos explican el desglose ("con cupón de 45 €,
  sin cupón: 275,49 €") y también avisan si el cupón desaparece (para ti es
  una subida de precio). Ojo: en Amazon el cupón hay que **marcarlo** en la
  ficha antes de comprar.
- **Comprobación del ASIN.** El precio solo se acepta si la página es de
  verdad la ficha de ese producto (el ASIN de la URL aparece en ella). Así una
  redirección a otra variante no se cuela como cambio de precio.

### Para que falle lo menos posible (2026-10-24)

- **Amazon se lee cada 12 h, no cada 6** (`AMAZON_CADA_HORAS`), contando desde
  la última lectura correcta. Menos visitas, menos CAPTCHA. Si una lectura
  falla, se reintenta en el siguiente ciclo (6 h), sin esperar a las 12.
- **Pausa de 60 s entre las dos fichas de Amazon** (`ESPERA_ENTRE_AMAZON_SEG`)
  para no pedirlas de golpe.
- **Solo avisa de un error tras 3 ciclos seguidos fallando**
  (`FALLOS_ANTES_DE_AVISAR`). Un CAPTCHA o un 429 sueltos se arreglan solos en
  el ciclo siguiente y no te llegan; si el problema persiste (~12-18 h), sí.

## Cómo son los mensajes

En cada comprobación llega **un solo mensaje** con todas las novedades,
agrupadas por categoría y con un bloque por producto (si no hay novedades, no
llega nada):

```
🔔 2 novedades

🪑 SILLAS
━━━━━━━━━━━━━━━

🔥 Sihoo B100 blanca · Amazon
¡Por debajo de tu aviso de 169,99 €!
💰 199,99 € → 159,99 €  (−40,00 €)
Ver producto

🧹 ASPIRADORAS
━━━━━━━━━━━━━━━

📉 Xiaomi G20 · Amazon
Ha bajado de precio
💰 125,00 € → 119,00 €  (−6,00 €)
🎯 Aviso de chollo: por debajo de 115,00 €
🎟️ Incluye cupón de 10,00 € (sin cupón: 129,00 €). Márcalo en la ficha al comprar.
Ver producto
```

Los emojis de cabecera dicen qué ha pasado de un vistazo: 🔥 por debajo de tu
aviso, 📉/📈 bajada/subida, ↗️ vuelve a subir por encima del aviso, 🟢/🛑 vuelve
el stock / se agota (solo tienda oficial), ⚠️ no se ha podido leer. El
resumen semanal ("📡 Sigo vigilando") usa la misma disposición. Si algún día
hay tantos productos que no cabe en un mensaje de Telegram, se parte por
categorías.

Si el envío a Telegram falla, no se marca nada como avisado: el aviso se
repite en la siguiente comprobación.

## Qué te avisa (y qué no)

| Evento | ¿Avisa? |
|---|---|
| Un precio baja de su umbral | ✅ Una vez, hasta que vuelva a subir |
| Un precio cambia (sube o baja) SIN cruzar el umbral | ✅ Con `NOTIFY_ON_ANY_CHANGE` (activado por defecto) — es el modo "seguimiento general": te enteras de cualquier movimiento de precio, no solo de las rebajas |
| Sihoo se agota / vuelve a haber stock | ✅ (solo Sihoo — Amazon no lo detecta, ver arriba) |
| En Amazon aparece o desaparece un cupón | ✅ Como cambio de precio (el precio vigilado es el final, con cupón) |
| Ha pasado una semana sin ningún aviso | ✅ Un único mensaje con el precio actual de **todos** los productos |
| Un producto falla al leer el precio | ✅ Solo si lleva **3 comprobaciones seguidas** fallando; después, máximo 1 vez / 24 h y **solo del que falla** |
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
    "id": "algo_unico",          # su memoria: no lo cambies después
    "categoria": "Aspiradoras",  # agrupa el producto en los mensajes
    "nombre": "Xiaomi G20",      # como aparece en los mensajes
    "tienda": "Amazon",
    "tipo": "amazon",            # o "shopify"
    "url": "https://www.amazon.es/dp/B0DJ82JKFL",
    "variant_id": "...",         # solo si tipo == "shopify"
    "umbral_eur": 125.00,
},
```

Todo lo demás (estado, avisos, historial, resumen semanal) funciona solo para
el producto nuevo. Una **categoría nueva** también: aparece en su propio
grupo, en el orden en que esté en `PRODUCTOS`. Para darle emoji, añádelo a
`EMOJI_CATEGORIA` en `check_price.py` (si no, sale con 📦).

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

## Desplegar en tu propio servidor (Raspberry Pi, con Docker)

Esto es lo que de verdad mantiene el bot vivo ahora. Se hace por SSH,
conectado a la Raspberry.

### 1. Copiar el proyecto a la Raspberry

Lo más simple es clonar el repo directamente ahí:

```bash
git clone https://github.com/balbibots/silla-ergonomica.git
cd silla-ergonomica
```

(Si prefieres no usar git en la Raspberry, también vale con copiar por
`scp`/`sftp` los archivos: `check_price.py`, `loop_runner.py`, `Dockerfile`,
`docker-compose.yml`, `.env.example`.)

### 2. Crear tu archivo de configuración

```bash
cp .env.example .env
nano .env
```

Rellena `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (los mismos de siempre, o
unos nuevos si prefieres separar avisos). Guarda con `Ctrl+O`, `Enter`,
`Ctrl+X`. Este archivo **nunca se sube a git** (ya está en `.gitignore`).

### 3. Construir y arrancar el contenedor

```bash
docker compose up -d --build
```

- `up` arranca el servicio.
- `-d` lo deja corriendo en segundo plano (puedes cerrar la sesión SSH y
  sigue funcionando).
- `--build` construye la imagen la primera vez (y cada vez que cambies el
  código).

**La primera vez tarda varios minutos**: descarga la imagen oficial de
Playwright (~1 GB) y ocupa ~2 GB en disco. Las siguientes reconstrucciones son
rápidas, porque esa base ya está en la Raspberry.

### 4. Comprobar que funciona

```bash
docker compose logs -f
```

Deberías ver algo como:

```
Bot arrancado en modo bucle. Comprobando cada 6.0 horas.

[2026-10-25T...] Nueva comprobacion...
  Sihoo: leyendo con navegador real (Chromium)
[Sihoo B100 blanca · Sihoo (tienda oficial)] 199.99 EUR | disponible: True | umbral: 199.99 EUR
  Amazon: leyendo con navegador real (Chromium)
[Sihoo B100 blanca · Amazon] 199.99 EUR | disponible: True | umbral: 169.99 EUR
  pausa de 60s antes de la siguiente ficha de Amazon...
  Amazon: leyendo con navegador real (Chromium)
[Flexispot ErgoX Lite beige · Amazon] 230.49 EUR (275.49 sin cupon, cupon 45,00 €) | disponible: True | umbral: 230.49 EUR
  pausa de 60s antes de la siguiente ficha de Amazon...
  Amazon: leyendo con navegador real (Chromium)
[Xiaomi G20 · Amazon] 125.00 EUR | disponible: True | umbral: 125.00 EUR
```

En los ciclos en los que a Amazon aún no le toca (cada 12 h), verás
`leido hace menos de 12 h, se salta este ciclo`.

`Ctrl+C` sale de los logs sin parar el contenedor (sigue corriendo detrás).

### Comandos que usarás a partir de ahora

| Quiero... | Comando |
|---|---|
| Ver qué está haciendo ahora mismo | `docker compose logs -f` |
| Pararlo | `docker compose down` |
| Arrancarlo nuevamente | `docker compose up -d` |
| Actualizar tras un cambio en el código (`git pull` primero) | `docker compose up -d --build` |
| Ver si está corriendo | `docker compose ps` |

### ¿Y si se reinicia la Raspberry?

Con `restart: unless-stopped` en el `docker-compose.yml`, Docker vuelve a
arrancar el contenedor solo, tanto si se cae por un error como si reinicias
la Raspberry entera — siempre que el propio Docker esté configurado para
arrancar al encender el sistema (lo normal en una instalación estándar).

### El estado sobrevive a todo esto

`state.json` no vive dentro del contenedor — vive en la carpeta `./data` de
la propia Raspberry (se crea sola la primera vez), gracias al volumen del
`docker-compose.yml`. Si reconstruyes la imagen, paras y arrancas el
contenedor, o incluso lo borras y lo vuelves a crear, esa carpeta se queda
intacta y el bot no "olvida" nada.

En esa misma carpeta está `historico_precios.csv`: una fila por cada lectura
correcta (fecha, producto, precio final, stock, precio sin cupón y cupón).
Puedes abrirlo con Excel o LibreOffice para ver cómo evoluciona cada precio:

```bash
tail -n 20 data/historico_precios.csv
```

### Quitar el bot y liberar el espacio

`docker compose down` solo borra el **contenedor**; la imagen (los ~2 GB) se
queda en disco. Para liberarlos, desde la carpeta del proyecto:

```bash
docker compose down --rmi local
```

```bash
docker image rm mcr.microsoft.com/playwright/python:v1.63.0-noble
```

```bash
docker builder prune -f
```

El primero borra el contenedor y la imagen construida; el segundo, la imagen
base de Playwright (si no la usa otro proyecto); el tercero, la caché de
construcción. Después puedes borrar la carpeta del proyecto, que incluye
`./data/state.json`.

## Estructura

```
check_price.py                      el script (libreria estandar; Playwright solo para leer Amazon)
loop_runner.py                      bucle que llama a check_price.py cada X horas (para Docker)
Dockerfile                          la imagen del contenedor (base oficial de Playwright: navegador incluido)
docker-compose.yml                  como se arranca (volumen, variables, reinicio automatico)
.env.example                        plantilla de configuracion (copiar a .env, con tus datos)
.github/workflows/check-price.yml   solo boton manual ahora (ver seccion de arriba)
data/state.json                     memoria entre ejecuciones cuando corre en Docker (se crea sola)
data/historico_precios.csv          todas las lecturas de precio (se crea sola)
state.json                          memoria de cuando corria en GitHub Actions (historico, ya no se actualiza)
```
