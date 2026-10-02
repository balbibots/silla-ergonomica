# Vigilante de precio — Sihoo Doro C300

Mismo tipo de bot que [yepoda-price-bot](https://github.com/balbibots/yepoda-price-bot),
pero generalizado para vigilar **varios productos de fuentes distintas** a la
vez. De momento vigila dos:

| Producto | Fuente | Último precio confirmado | Aviso si baja de |
|---|---|---|---|
| Sihoo Doro C300, blanca | [Tienda oficial](https://eu.sihoo.com/es/products/sihoo-doro-c300-ergonomischer-stuhl) | 299,99 € (2026-09-30) | 299,99 € |
| Sihoo Doro C300, gris | [Amazon.es](https://www.amazon.es/SIHOO-Doro-C300-ergon%C3%B3mica-reposabrazos/dp/B0C3TNC785) | 336,99 € (2026-09-30 — ¡ya por debajo del umbral!) | 379,99 € |

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

### 4. Comprobar que funciona

```bash
docker compose logs -f
```

Deberías ver algo como:

```
Bot arrancado en modo bucle. Comprobando cada 6.0 horas.

[2026-09-30T...] Nueva comprobacion...
[Sihoo Doro C300 (tienda oficial, blanca)] 299.99 EUR | disponible: True | umbral: 299.99 EUR
[Sihoo Doro C300 (Amazon.es, gris)] 336.99 EUR | disponible: True | umbral: 379.99 EUR
```

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

## Estructura

```
check_price.py                      el script (solo libreria estandar)
loop_runner.py                      bucle que llama a check_price.py cada X horas (para Docker)
Dockerfile                          la imagen del contenedor
docker-compose.yml                  como se arranca (volumen, variables, reinicio automatico)
.env.example                        plantilla de configuracion (copiar a .env, con tus datos)
.github/workflows/check-price.yml   solo boton manual ahora (ver seccion de arriba)
data/state.json                     memoria entre ejecuciones cuando corre en Docker (se crea sola)
state.json                          memoria de cuando corria en GitHub Actions (historico, ya no se actualiza)
```
