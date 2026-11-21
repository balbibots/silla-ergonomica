# Vigilante de precios

Bot personal que corre en Docker en una Raspberry Pi 5 y avisa por Telegram
(o Discord) cuando pasa algo relevante con un precio. Coste de funcionamiento:
**0 €**. Funciona solo: no hay que exportar archivos ni hacer nada a mano para
que siga funcionando.

Tiene dos modos independientes:

1. **Seguimiento:** los productos que tú eliges, en Amazon y en otras tiendas
   españolas (Powerplanet, MediaMarkt, tiendas oficiales). Cuenta el mejor
   precio con stock entre todas.
2. **Descubrimiento:** ofertas nuevas por categorías, a partir de los datos
   abiertos de [CholloMazon](https://chollomazon.com/datos) (CC BY 4.0).

El bot trabaja en silencio. No manda mensajes de "comprobación hecha", de
precio sin cambios, de reinicio ni de errores puntuales: todo eso va a los
logs y a la base de datos.

## Cuatro niveles de seguimiento

| Nivel | Qué vigila | Cuándo avisa | Fuentes |
|---|---|---|---|
| **1 · Tus productos** | Los productos de `productos`, en cada una de sus tiendas | **Cada transición**: cualquier cambio de precio (desde 0,01 €) o de stock en cada tienda, aunque no sea la más barata y aunque ese precio ya se viera antes; 📉/📈 cambio del mejor precio, 🎯 objetivo, 📊/🏆 mínimos y 📦 reposición. Todo lo de un ciclo va en **una sola tarjeta** por producto. Sin filtros de valoraciones | Fichas de las tiendas (y pccomtracker para PcComponentes). 👀 también si alguien los publica en Chollometro |
| **2 · 🛍️ Intereses activos** | Lo que buscas ahora (`descubrimiento.intereses`, de inicio: aspiradoras) por palabras del título, con exclusiones (robots, recambios, bolsas…) | Descuento de al menos un 15 % y 10 €, **o** precio por debajo de tu objetivo (140 €), entre 50 y 250 €. Lo que sale de un catálogo (una categoría o una búsqueda) necesita siempre descuento frente a una referencia real | CholloMazon, Chollometro y, para tus intereses, las categorías de pccomtracker y las búsquedas de reacofertas |
| **3 · 💥 Chollazos** | Tus temas | Descuento **real** de al menos un **60 % y 30 €** sobre algo que valga 50 € o más. Real = frente a precios cobrados de verdad: el precio habitual de pccomtracker, la mediana diaria de 90 días de reacofertas o lo que el bot ha visto durante al menos un mes. **Un tachado, un PVPR o el «antes» de una publicación nunca bastan** | pccomtracker, reacofertas y CholloMazon (con historial del bot) |
| **4 · 🚨 Errores de precio** | **Cualquier categoría** | Descuento del **80 %** o más (sobre algo de 50 € a 3.000 €, ahorrando 30 €, y no más del 95 %, que suele ser un dato roto), o la comunidad lo llama «error de precio» en el título. A cualquier hora y sin gastar el cupo diario. **«Comprobado»** si el bot ha leído ese precio y stock en la ficha de la tienda (hoy, Amazon); si no, **«sin comprobar»** | Las cuatro fuentes |

Reglas comunes:

- Una oferta que encaja en varios niveles sale **una vez**, con el más fuerte
  (error > mínimo > chollazo > interés) y la nota "🛍️ De tu lista" si además es
  de tus intereses. Nunca se repite el mismo producto al mismo precio.
- Solo producto **nuevo**: fuera reacondicionado, segunda mano, usado, Amazon
  Renewed/Warehouse u "open box", también si solo lo dice el título.
- La variante se comprueba por EAN en las tiendas y por ASIN en Amazon; un
  producto que sigues no vuelve a salir como descubrimiento.
- Un fallo de lectura no es "sin stock" y nunca genera un aviso.
- Si no ha cambiado nada, no hay mensaje.

Los descuentos de los niveles 2 a 4 son frente a un precio de referencia o
anterior, **no** frente a un histórico; la tarjeta lo dice. Los mínimos
(📊/🏆) solo salen con historial suficiente.

## Estado (noviembre de 2026)

| Función | Estado |
|---|---|
| Seguimiento de varios productos (añadir y quitar por ASIN o URL) | ✅ Operativo |
| Un producto en varias tiendas, avisando con el mejor precio con stock | ✅ Operativo: Amazon, Powerplanet, MediaMarkt y la tienda oficial de Dreame. Probado con lecturas reales |
| PcComponentes | ✅ Seguida, leyendo su precio en pccomtracker.com (PcComponentes bloquea al bot). Probado con la G20 y la U20 reales |
| Histórico de tiendas de terceros (reacofertas 180 d, pccomtracker mínimo del año) | ✅ Operativo para PcComponentes y Powerplanet; MediaMarkt aún no aparece en reacofertas para estas fichas |
| Nuevo, segunda mano, reacondicionado y agotado bien distinguidos | ✅ Operativo (comprobado en la Pi con la G20) |
| Histórico en SQLite, migración del CSV y CSV compatible | ✅ Operativo (198 filas migradas en la Pi) |
| Descubrimiento automático por categorías con CholloMazon | ✅ Probado con el CSV real; pendiente de su primera semana en la Pi |
| Lectura extra cuando un producto que sigues aparece rebajado | ✅ Operativo |
| Tarjetas en Telegram (foto y botón) | ✅ Operativo |
| Tarjetas en Discord (embed) | ⏳ Hecho y probado en simulación, falta un webhook real |
| Mínimo de 365 días de Amazon | 🟡 Opcional: una importación por producto, ya no mensual |
| Alertas de Keepa como fuente | ⏳ Posible por correo; falta un correo de muestra (ver abajo) |
| Comprobación cada 15 minutos | ❌ Ninguna fuente lo autoriza (ver "Frecuencia") |

## Fuentes y autorización

Lo que se investigó y comprobó el 21/11/2026:

| Fuente | Qué da | ¿Se usa? | Por qué |
|---|---|---|---|
| **Ficha de Amazon** (`/dp/ASIN`) | Precio, condición, vendedor, imagen y valoraciones | Sí: seguimiento y verificación de ofertas | Un Chromium real, sin sesión y a baja frecuencia. Las [Condiciones de Uso](https://www.amazon.es/gp/help/customer/display.html?nodeId=201909000) prohíben la "extracción sistemática" de "partes sustanciales" sin permiso escrito. Una ficha cada pocas horas para uso propio no busca eso, pero Amazon no autoriza expresamente ningún acceso automatizado |
| **CSV de CholloMazon** | Unas 160 ofertas activas con ASIN, precio, precio de referencia, cupón y fechas | Sí: descubrimiento | [Licencia CC BY 4.0](https://chollomazon.com/datos) ("puedes usarlos citando a CholloMazon con un enlace a esta página"). Su robots.txt lo permite y el CSV se refresca cada hora (`max-age=3600`). Cada tarjeta lleva la atribución |
| **RSS de CholloMazon** (`/feed.xml`) | 50 ofertas, sin ASIN | No | La licencia está declarada para el CSV, que además trae el ASIN |
| **RSS de Chollometro** (`/rss/nuevos`) | 30 ofertas de todas las tiendas (20 de Amazon), sin ASIN | No | Sin ASIN no se puede comprobar ni cruzar con nada. Su [aviso legal](https://www.chollometro.com/p%C3%A1gina/aviso-legal) prohíbe reproducir, copiar o "poner a disposición de terceros" sus ofertas sin autorización |
| **Historial de 365 días de Amazon** | Precio más bajo de cada día, sin envío | Solo por importación | **Comprobado:** sin sesión iniciada no aparece el acceso al historial (probado con dos productos). Llega por un canal interno del asistente, con cookies de sesión y un token anti-CSRF |
| **Cuenta de Amazon dedicada al bot** | El mismo historial, automatizado | No | Abrirlo con un robot es la extracción automatizada que prohíben las condiciones, con cualquier cuenta. Además, Amazon podría cerrarla |
| **Amazon Creators API** | Catálogo, ofertas y búsqueda por categoría | No, de momento | Gratis, pero [exige](https://affiliate-program.amazon.com/creatorsapi/docs/) ser afiliado con 10 ventas en 30 días. No da histórico |
| **Alertas de Keepa** | Aviso cuando un producto baja de tu umbral o vuelve el stock | Posible (pendiente) | Gratis por correo, Telegram, notificaciones web o su app ([ayuda](https://keepa.com/#!support)). Sus [condiciones](https://keepa.com/#!terms) prohíben el scraping, pero no "una funcionalidad que el propio proveedor ofrece para ese fin" |
| **Alertas de CamelCamelCamel** | Lo mismo, por correo | No | Sus [condiciones](https://es.camelcamelcamel.com/tos) prohíben la recogida automática de datos "from our website and/or emails" |
| **Keepa API** | Histórico completo | No | Es de pago |

### Tiendas españolas (comprobado el 21/11/2026)

| Tienda | ¿Se puede leer el precio? | Cómo | Historial | Por qué |
|---|---|---|---|---|
| **Powerplanet** | ✅ Sí | Datos estructurados schema.org (JSON-LD) de la ficha: precio, stock, condición, vendedor y EAN. Una petición, sin navegador | No | Su robots.txt permite las fichas |
| **MediaMarkt** | ✅ Sí | Igual, JSON-LD de la ficha. Muestra si lo vende MediaMarkt o un vendedor externo del marketplace | No | Su robots.txt lo permite todo. Sus [condiciones de uso](https://www.mediamarkt.es/es/legal/condiciones-de-uso-de-la-web) prohíben copiar contenidos "para propósitos públicos o comerciales" y saltarse medidas de seguridad: un aviso privado no hace ninguna de las dos cosas |
| **Tienda oficial de Dreame** (Shopify) | ✅ Sí | El JSON público del producto (`/products/<ficha>.js`): precio, stock y EAN | No | Su robots.txt permite `/products/` |
| **PcComponentes** | 🟡 Indirectamente | La ficha pública de **pccomtracker.com** del producto: su JSON-LD trae el precio y el stock de PcComponentes, la URL exacta de la ficha de PcComponentes (así se comprueba que es la misma) y la hora de su última comprobación. Un dato de más de 26 h no se usa | reacofertas (180 d) y pccomtracker (mínimo del año) | PcComponentes responde 403 (Cloudflare) a urllib y a un Chromium automatizado; su robots.txt veta el agente `Python-urllib` y sus rutas de histórico y API. No se intenta superar. pccomtracker permite sus fichas en su robots.txt (solo veta `/api/`, `/admin/` y `/goto/`); no publica condiciones de uso |
| **Tienda de Xiaomi** (mi.com) | ❌ No | — | No | La ficha no publica el precio en datos estructurados. Va como enlace |

Para no comparar churras con merinas, **cada ficha se comprueba con el EAN** del
producto. Si una tienda tiene otra variante u otro color, da error en vez de
colar un precio que no toca. Por eso la ficha de MediaMarkt que parece la R20 SE-A
no se usa: su EAN (…568) no es el de la R20 SE-A (…575).

**Descubrimiento en estas tiendas:** no hay una vía autorizada hoy.

- **MediaMarkt:** sus páginas de ofertas no publican la lista de productos en
  datos estructurados; los precios llegan por su API interna (`/api/v1/graphql`),
  que no es pública.
- **PcComponentes:** bloquea la lectura, y sus API de búsqueda y de precio están
  prohibidas en su robots.txt.
- **Vía oficial posible:** PcComponentes y Powerplanet tienen programa de
  afiliados en [Awin](https://ui.awin.com/merchant-profile-terms/20982), y Awin
  ofrece a los afiliados aceptados feeds de producto con precio y stock.
  Powerplanet anuncia un feed automatizado. Requiere darse de alta y ser
  aceptado, así que queda pendiente de tu decisión.

### Alertas de Keepa (opcional)

Las alertas gratuitas de Keepa usan su propio historial, más completo que el
del bot. Hay dos formas de aprovecharlas:

- **Sin tocar nada del bot:** configura el seguimiento de cada producto en
  Keepa una vez, con aviso por Telegram. Llega por el bot de Keepa, no a
  Discord.
- **Integrado en el bot (pendiente):**
  - Keepa enviaría las alertas por correo a un buzón solo para esto, y el bot
    lo leería por IMAP para convertirlas en tarjetas.
  - Para hacerlo bien hace falta un correo de alerta real de muestra: no se
    inventa el formato.
  - El buzón y su contraseña de aplicación los creas tú. Irían en `.env`, nunca
    en el código.

## Frecuencia

- **Seguimiento:** cada 2 horas por producto (`intervalo_min_por_fuente`).
  - **Antes eran 12 h:** se fijaron en octubre de 2026 como precaución tras
    los CAPTCHA del modo HTTP ("menos visitas, menos CAPTCHA"). No salían de
    ningún límite medido: **Amazon no publica ninguno** y no autoriza el
    acceso automatizado a ningún ritmo. Con 12 h, un stock que dura poco podía
    aparecer y agotarse entre dos lecturas.
  - **Si aparece un CAPTCHA,** el bot espera cada vez más (30 min, 1 h, 2 h)
    sin insistir.
  - **Funcionamiento actual:** desde que se lee con el navegador real (24/10),
    los logs revisados de noviembre no muestran ningún CAPTCHA a 12 h.
- **Lectura extra:** se adelanta la lectura cuando hay una señal real. Si un
  producto que sigues aparece en CholloMazon más barato que tu última lectura,
  se lee en Amazon en la siguiente vuelta. Entre dos lecturas extra pasan al
  menos 60 minutos (`lectura_extra_min_entre`).
- **Otros ritmos:**
  - Con `"amazon_ficha": 60` (1 h) son 24 visitas al día por producto.
  - Cada 15 minutos serían 96 al día por producto. No se recomienda: subiría
    mucho el riesgo de CAPTCHA y se acercaría a la extracción sistemática.
- **Para enterarte al momento de una reposición,** lo más rápido es la alerta
  gratuita de stock de Keepa a tu Telegram (ver "Alertas de Keepa").
- **Descubrimiento:** CholloMazon como mucho una vez por hora, que es lo que
  dura su caché.
- **Verificación de ofertas:** hasta 20 fichas de Amazon al día, con 60 s
  entre una y otra.

## Configuración

`data/config.json` se crea solo la primera vez, copiando
[`config.example.json`](config.example.json). Si es de una versión anterior,
se actualiza solo, con copia de seguridad (`config.json.v1.bak`,
`config.json.v2.bak`) y sin tocar tus productos. Se puede editar sin tocar código: el bot lo relee en cada
vuelta y lo valida entero.

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| `canal` | `telegram` | `telegram`, `discord` o `consola` (solo escribe en el log) |
| `seguimiento.intervalo_min_por_fuente` | Amazon 120, resto 30 | Minutos entre lecturas de cada producto |
| `seguimiento.reglas.avisar_cambios` / `cambio_min_eur` | `true` / `0.01` | Avisa de cada bajada 📉 y subida 📈 del mejor precio |
| `seguimiento.reglas.avisar_cambios_por_tienda` | `true` | Avisa también de los cambios de precio y stock de cada tienda, aunque no sea la más barata (🔄) |
| `descubrimiento.intereses` | Aspiradoras | Lista de intereses activos: `nombre`, `incluir`, `excluir`, `precio_min`, `precio_max`, `precio_objetivo`, `descuento_min_pct`, `ahorro_min_eur`, `activo`. Añade los que quieras |
| `descubrimiento.errores` | 80 % / 30 € / 50 € / 3.000 € / 95 % / todas | Nivel 4: `descuento_min_pct`, `ahorro_min_eur`, `valor_min_eur`, `max_referencia_eur`, `max_descuento_pct` (por encima, dato roto), `todas_las_categorias` |
| `descubrimiento.valoracion_min` / `valoraciones_min` / `valoraciones_en` | 4 / 25 / chollo, interes, minimo | Estrellas y opiniones mínimas en Amazon, y en qué niveles se exigen. Nunca en errores de precio ni en tus productos |
| `descubrimiento.fuentes.pccomtracker` / `reacofertas` | cada 60 / 180 min, 25 / 20 fichas | Descubrimiento con referencia real. `max_fichas`: fichas nuevas por pasada (cada ficha se recuerda 6 h) |
| `intereses[].pccomtracker_categorias` / `reacofertas_busquedas` | aspiradoras-escoba / «aspiradora escoba», «aspirador sin cable» | Dónde buscar cada interés en esas webs |
| `seguimiento.lectura_extra_min_entre` | `60` | Mínimo de minutos entre lecturas extra |
| `seguimiento.reglas.bajada_pct` / `bajada_eur` | `5` / `10` | Bajada que merece aviso: 5 % **o** 10 € |
| `seguimiento.reglas.min_observaciones_minimo` / `min_dias_minimo` | `10` / `14` | Historial que hace falta para un mínimo local |
| `productos[]` | G20, U20 y R20 SE-A | `id`, `nombre`, `categoria`, `precio_objetivo`, `ean`, `tiendas` (cada una con `tienda` y `asin` o `url`) y `enlaces` (tiendas que solo se enlazan). Opcionalmente, `bajada_pct` y `bajada_eur` propios. El formato antiguo (solo `asin`) sigue valiendo |
| `descubrimiento.activo` | `true` | Activa el modo 2 |
| `descubrimiento.fuentes.chollomazon` | cada 60 min, datos de menos de 48 h | Fuente de ofertas |
| `descubrimiento.categorias` | Electrónica, Informática, Gaming, Hogar, Herramientas | Tus temas en CholloMazon (o `"Hogar > Cocina"` para una subcategoría) |
| `descubrimiento.comunidad.categorias` | Electrónica, Gaming, Hogar, vivienda y oficina | Tus temas en Chollometro. El resto (moda, belleza, juguetes, comida...) no avisa |
| `descubrimiento.comunidad.palabras_error` | "error de precio", "fallo de precio"... | Solo cuentan en el título |
| `descubrimiento.comunidad.valor_min_eur` / `ahorro_min_eur` / `descuento_min_pct` | 50 / 30 / 60 | Un chollazo de Chollometro necesita valer al menos 50 €, ahorrar 30 € y un 60 % |
| `descubrimiento.comunidad.precio_min_error_eur` | 10 | Un error de precio sin precio anterior tiene que costar al menos esto |
| `productos[].en_comunidad` | `incluir` / `excluir` | Palabras para reconocer tu producto en Chollometro sin confundir modelos (la G20 excluye "lite" y "max") |
| `descubrimiento.excluir_categorias`, `marcas`, `palabras_clave`, `excluir_palabras` | vacías | Más filtros |
| `descubrimiento.precio_min` / `precio_max` | `25` / `600` | Rango de precio |
| `descubrimiento.avisar` | los tres | Qué niveles se notifican |
| `descubrimiento.oferta_interesante` | 60 % y 30 € | Chollazo de CholloMazon: descuento mínimo frente al precio de referencia |
| `descubrimiento.minimo_local` | 5 observaciones, 30 días | Historial que hace falta para un mínimo local de una oferta |
| `descubrimiento.verificar_en_amazon` | `true` | Comprobar precio, stock nuevo y valoraciones antes de avisar |
| `descubrimiento.max_verificaciones_dia` | `20` | Fichas de Amazon al día para verificar |
| `descubrimiento.valoracion_min` / `valoraciones_min` | `4.0` / `25` | Calidad mínima (se mira en Amazon) |
| `descubrimiento.max_avisos_ejecucion` / `max_avisos_dia` | `5` / `15` | Límites de avisos |
| `descubrimiento.horario` | 08:00-23:00 | Fuera de horario se registra pero no se avisa |

Categorías que usa CholloMazon en noviembre de 2026 (sacadas de su propio
CSV, no inventadas): Electrónica, Informática, Hogar, Gaming, Deporte, Moda,
Belleza, Herramientas, Bebé, Productos de Limpieza y Otros. Algunas tienen
subcategoría, como "Hogar > Cocina".

Los secretos van en `.env` ([`.env.example`](.env.example)), nunca en el
JSON.

### Precios e históricos de terceros

**Descubrimiento con referencia real (v9).** Además del seguimiento, el bot
lee en pccomtracker la portada («Mayores bajadas de hoy», «Mejores ofertas»)
y las categorías de tus intereses, y en reacofertas su lista de bajadas y sus
búsquedas. De cada candidato lee su ficha: la referencia es el precio
habitual de pccomtracker o la **mediana diaria de 90 días** de reacofertas
(cada día cuenta con el precio que tuvo, así un pico de un día no la mueve).
Los datos rotos que publican (un adaptador «a 12.098 €», una tapa de objetivo
«a 1.227 €») se descartan por los topes de referencia y de descuento. Sus
enlaces de compra son de afiliado: el botón lleva a la ficha de PcComponentes
(pccomtracker la da) o a la página de reacofertas.

Dos webs españolas siguen PcComponentes (y otras tiendas) a diario y publican
sus datos en páginas que su robots.txt permite. El bot las usa con peticiones
normales identificadas como el bot (`vigilante/terceros.py`):

| Web | Qué se usa | Cobertura | Cada cuánto |
|---|---|---|---|
| [pccomtracker.com](https://pccomtracker.com/) | Precio y stock actuales de PcComponentes (JSON-LD) y, de su FAQ, el mínimo del último año con fecha y el precio habitual | PcComponentes (y su comparación con Amazon) | Precio: cada 60 min. Mínimo: cada 12 h |
| [reacofertas.es](https://reacofertas.es/tracking-precios) | La serie de los últimos 180 días del gráfico (`?q=<URL de la tienda>`) | PcComponentes, MediaMarkt y Powerplanet (unos 160 000 productos) | Cada 12 h |

- La URL de pccomtracker se pone una vez por producto (`"pccomtracker"` en la
  tienda). Su buscador va por `/api/`, que su robots.txt veta, así que no se usa.
- Una serie de reacofertas solo se guarda si su último precio coincide (±3 %)
  con el que el bot acaba de leer en esa tienda: si no, es otra ficha.
- Las tarjetas muestran esos mínimos como referencia, con su fuente y fecha
  ("📊 Mínimo 180 d en PcComponentes según reacofertas: 169,99 € (07/11/2026)").
  No cambian qué se avisa: los avisos siguen dependiendo de tus lecturas.
- Las dos webs no siempre coinciden en la fecha del mínimo (reacofertas
  sincroniza una vez al día); por eso se cita cada una por separado.

### Un producto en varias tiendas

```json
{
  "id": "dreame-u20",
  "nombre": "Dreame U20",
  "categoria": "Aspiradoras",
  "precio_objetivo": 100.0,
  "ean": "6976233670607",
  "tiendas": [
    {"tienda": "Dreame", "url": "https://es.dreametech.com/products/aspirador-sin-cable-u20"},
    {"tienda": "MediaMarkt", "url": "https://www.mediamarkt.es/es/product/_aspirador-escoba-dreame-308668681-370-w-60-min-negro-139804042.html"},
    {"tienda": "PcComponentes", "url": "https://www.pccomponentes.com/dreame-u20-aspiradora-escoba-mano-sin-cable-370w-blanca",
     "pccomtracker": "https://pccomtracker.com/producto/dreame-u20-aspiradora-escobamano-sin-cable-370w-blanca-20128212"}
  ]
}
```

El bot deduce cómo leer cada tienda:

- `asin`: Amazon.
- URL con `/products/`: Shopify.
- El resto: JSON-LD.

Si una tienda se bloquea (403, 429, desafío), se registra como fallo y se
reintenta más tarde, sin insistir. Para comparar solo cuentan las lecturas
recientes (menos de dos intervalos).

**Cómo decide con varias tiendas:**

- **Precio que cuenta:** el más bajo con unidades nuevas.
- **Bajada:** se mide frente al mejor precio anterior entre todas las tiendas.
- **Mínimo registrado:** el más bajo visto en cualquiera de sus tiendas.
- **Reposición:** que vuelva a haber unidades en alguna.
- **Tarjeta:** lleva el botón a la tienda más barata, el resto de tiendas con
  su precio y botones para las tiendas que solo se enlazan.

### Añadir y quitar productos

```bash
docker compose exec silla-sihoo-bot python3 -m vigilante anadir "https://www.amazon.es/dp/B0DJ82JKFL" --nombre "Xiaomi G20" --categoria Aspiradoras --objetivo 125
docker compose exec silla-sihoo-bot python3 -m vigilante quitar B0DJ82JKFL
docker compose exec silla-sihoo-bot python3 -m vigilante listar
docker compose exec silla-sihoo-bot python3 -m vigilante estado
```

`anadir` añade un producto solo de Amazon. Para varias tiendas, edita
`data/config.json` como en el ejemplo de arriba. Se valida el ASIN (solo
`amazon.es`), el formato de cada tienda y que no haya duplicados. `quitar`
acepta el `id` del producto o su ASIN. Al quitar un producto, su histórico se
conserva.

## Cómo decide

### Productos que sigues

Un aviso por acontecimiento. Si coinciden varios, sale uno solo con el motivo
principal y los demás como notas:

| Prioridad | Aviso | Cuándo |
|---|---|---|
| 1 | 🏆 Mínimo histórico verificado · 365 días | Por debajo del mínimo del gráfico de 365 días importado y de todo lo leído desde entonces |
| 2 | 📊 Mínimo del histórico local | Por debajo de todo lo que el bot ha visto, con historial suficiente |
| 3 | 🎯 Precio objetivo alcanzado | El precio a pagar (con cupón, si lo hay) llega a tu objetivo. Se rearma al superarlo |
| 4 | 📉 Bajada de precio | Al menos 5 % o 10 € frente a la última lectura válida |
| 5 | 🔁 Vuelve a su mínimo del histórico local | Solo después de haber subido |
| 6 | 📦 De nuevo en stock | Vuelve a haber unidades **nuevas** |

No avisa:

- de que se agota (se registra y se espera a la reposición);
- de las ofertas de segunda mano o reacondicionadas (se guardan aparte);
- de una primera lectura como "récord";
- del mismo evento al mismo precio dentro de 24 h.

Un aviso que no se puede enviar queda pendiente y se reintenta en la
siguiente vuelta.

### Descubrimiento

1. **Descarga:** baja el CSV de CholloMazon (como mucho cada hora, con
   petición condicional y respetando `Retry-After`).
2. **Histórico local:** guarda el precio de cada oferta, solo si cambia o ha
   pasado un día.
3. **Filtros:** categoría, precio, marca, palabras, que el dato tenga menos de
   48 h y solo producto nuevo.
4. **Nivel:** clasifica cada oferta en uno de los tres niveles, del más fiable
   al menos fiable.
5. **Orden:** ordena por nivel y ahorro.
6. **Comprobación en Amazon:** la hace con los finalistas. Descarta los que ya
   no tienen unidades nuevas, han subido de precio o tienen menos de 4
   estrellas o menos de 25 valoraciones.
7. **Aviso:** como mucho 5 por ejecución y 15 al día, y nunca dos veces el
   mismo producto al mismo precio. Lo descartado se guarda en la tabla
   `candidatos` con el motivo.

**Desde la primera descarga** se avisa de las ofertas que ya están activas:
primero las mejores, dentro de los límites, y nunca dos veces la misma al mismo
precio. Lo que no cabe en una ejecución sale en las siguientes (si el CSV no
ha cambiado, se revisa la última versión guardada). Los 30 días de historial
solo hacen falta para el aviso de "mínimo del histórico local"; las ofertas
interesantes no lo necesitan.

**Cómo se calcula:**

- **Precios:** todo en céntimos. Cada serie compara el mismo ASIN, la condición
  "nuevo" y el mismo tipo de precio (oferta destacada de Amazon, o precio de
  oferta de la fuente).
- **Mínimo de 365 días:** el gráfico de Amazon muestra el precio más bajo de
  cualquier oferta de cada día, que nunca es mayor que el de la oferta
  destacada. Si el precio queda por debajo, es un mínimo seguro. No se usa si
  el bot ha dejado más de 2 días sin leer el producto desde la importación.
- **Media de 90 días:** solo si hay lecturas en al menos el 90 % de los días.
  Si no, no aparece.

### Chollometro: errores de precio y tus productos

El RSS público de chollos nuevos (`/rss/nuevos`, unas 30 publicaciones cada
cuarto de hora) se lee cada 5 minutos. Trae título, tienda, precio, categoría,
foto y la descripción; no trae ASIN ni precio anterior estructurado.

- **🚨 Error de precio:** la comunidad lo dice en el título, o el descuento
  frente al precio anterior de la publicación es del 80 % o más. En cualquier
  categoría, pero no calderilla (al menos 10 €, o 30 € de ahorro sobre algo de
  50 € si la publicación da el precio anterior). Sale a cualquier hora.
- **💥 Chollazo:** la publicación da el precio de antes (tachado, "antes",
  "PVP"...) y el descuento es del 60 % o más, con al menos 30 € de ahorro sobre
  algo de 50 € o más, en tus temas. Respeta el horario.
- **🛍️ Interés activo:** encaja en tu lista de intereses (p. ej. aspiradoras).
- **👀 Tu producto:** si el título nombra uno de tus productos (con sus palabras
  de `en_comunidad`), avisa siempre, en cualquier tienda. Así llegan también
  las ofertas de PcComponentes, que el bot no puede leer.

Casos reales que ya no avisan: unos mini cepillos de 3,95 € (la descripción
decía "me parece un preciazo") y un polo infantil de 3,09 € (antes 14,90 €).

La comunidad no verifica precios: la tarjeta lo indica y enlaza la publicación.

### Formato

Cada tarjeta se lee de un vistazo: qué pasa y con qué producto, el precio con
su tienda y su diferencia, **una** línea sobre su historia y, si hay más
tiendas, cómo están. La hora y la fuente van en la última línea.

```text
📉 Bajada de precio · Dreame U20

💶 99,00 € en PcComponentes · antes 119,00 € (−20,00 € · −17 %)
🏆 Iguala su mínimo del año en PcComponentes (19/11)
🏬 Dreame sin stock · MediaMarkt 209,00 €
🕘 21/11 22:56 · vía pccomtracker

[🛒 Ver en PcComponentes]
```

```text
🔄 Cambio en una tienda · Dreame U20

💶 189,99 € en MediaMarkt · antes 209,00 € (−19,01 € · −9 %)
🏆 Lo más barato ahora: 99,00 € en PcComponentes
🕘 21/11 23:56 · ficha de MediaMarkt
```

```text
🚨 Posible error de precio · Monitor LG UltraGear 27" 4K 144 Hz

💶 49,99 € en PcComponentes · antes 499,99 € (−450,00 € · −90 %)
ℹ️ Precio de antes según la publicación, sin verificar
⚡ Puede ser un error de precio: suele durar poco
🕘 21/11 21:56 · Chollometro · publicado hace 4 min
```

(Salen del código real con precios de ejemplo; el precio de antes va tachado.)

- La línea de historia usa la mejor evidencia disponible: el mínimo del año
  (pccomtracker), el de 6 meses (reacofertas) o lo más barato que ha visto el
  bot si lleva siguiéndolo al menos 14 días. Si no hay nada fiable, no se
  pone nada.
- "vende X" solo aparece si el vendedor no es la propia tienda (marketplace).

- **Telegram:** `sendPhoto` con la foto del producto y un botón real «🛒 Ver
  en Amazon».
  - Si Telegram rechaza la foto, manda el mismo texto con `sendMessage`.
  - Si el error es de red, no lo reenvía, para no duplicarlo.
  - El texto se recorta para no pasar de 1024 caracteres.
- **Discord:** webhook con embed: borde de color según el aviso, título
  enlazado a Amazon, miniatura y pie con fuente y condiciones. Las menciones
  están desactivadas y se respeta el 429. El enlace va en el título y como
  «🛒 Ver en Amazon» en el texto, porque Discord no documenta botones en un
  webhook normal.

Para usar Discord:

1. En el canal privado: Editar canal → Integraciones → Webhooks → Nuevo
   webhook → Copiar URL.
2. Pon la URL en `.env` como `DISCORD_WEBHOOK_URL=...`.
3. Cambia `"canal": "discord"` en `data/config.json`.

El bot nunca cambia de canal solo ni envía a los dos.

## Historial de 365 días (opcional, una vez por producto)

No hace falta para que el bot funcione. Sirve para que los mínimos de un
producto que sigues se comparen con todo el año desde el primer día, y no
solo con lo que el bot ha visto.

Se importa **una sola vez por producto**. Desde entonces, el bot completa la
ventana de 365 días con sus propias lecturas. Solo habría que repetirlo si el
bot deja más de 2 días seguidos sin leer ese producto, por ejemplo con la Pi
apagada. El estado lo dice.

1. En tu navegador, con tu sesión, abre la ficha, pulsa **Historial de
   precios** y espera al gráfico.
2. En DevTools → Network → «Save all as HAR».
3. Cópialo a la Pi e impórtalo:

   ```bash
   docker compose exec silla-sihoo-bot python3 -m vigilante importar-historial data/historial.har --asin B0DJ82JKFL
   rm ~/homelab/bots/silla-ergonomica/data/historial.har
   ```

Del HAR solo se lee el JSON del gráfico; las cookies y las cabeceras se
ignoran y no se guardan. **Borra el HAR después**: contiene tu sesión. Si el
producto solo tiene ofertas de segunda mano, Amazon no muestra histórico de
nuevo.

## Datos

En `data/`, en la propia Pi:

- **`precios.db` (SQLite):** observaciones (del seguimiento y de cada fuente,
  por separado), fallos aparte, estado de cada producto, avisos enviados,
  historiales importados y candidatos descartados con su motivo.
- **`historico_precios.csv`:** se sigue rellenando con las mismas columnas de
  siempre.
- **Migración:** el CSV antiguo se migró a SQLite, con copia en
  `data/copias/`. `python3 -m vigilante migrar` lo comprueba otra vez sin
  duplicar nada.
- **Exportación completa:** `python3 -m vigilante exportar-csv data/exportacion.csv`.

## Pruebas

```bash
python -m unittest discover -s tests -v
```

Son 101 pruebas sin red. Cubren:

- **Lectura de la ficha:** producto y variante correctos, precio de oferta
  frente al tachado y las cuotas, decimales con coma, página sin precio,
  CAPTCHA con HTTP 200, cupones, y nuevo frente a usado aunque la ficha diga
  "En stock".
- **Reglas de aviso:**
  - mínimos locales y externos, nuevos e igualados, y la falta de historial;
  - huecos de lectura y caché;
  - un solo aviso cuando coinciden varios, y sin duplicados;
  - ciclos sin cambios sin avisos, y persistencia tras reiniciar;
  - avisos pendientes.
- **Descubrimiento:**
  - formato real del CSV de CholloMazon, 304 y `Retry-After`;
  - avisos desde la primera descarga e intervalo de la fuente;
  - comprobación en Amazon, límites y los tres niveles;
  - lectura extra de los productos que sigues.
- **Chollometro:** formato real del RSS, errores de precio solo en el título,
  calderilla y temas que no te interesan fuera, chollazos del 60 %, errores del 80 % en cualquier categoría,
  intereses activos (aspiradoras sí; robots, bolsas o fuera de precio no),
  reacondicionados fuera, tus
  productos sin confundir modelos (G20, G20 Lite, G20 Max).
- **Terceros:** precio de PcComponentes en pccomtracker (otra ficha, dato
  antiguo, agotado), mínimo y precio habitual de su FAQ, serie de reacofertas
  y su comprobación contra la lectura, refresco cada 12 h y cita en la tarjeta.
- **Varias tiendas:** JSON-LD de Powerplanet y MediaMarkt, Shopify, EAN de otra
  variante rechazado, segunda mano, desafío anti-bots, PcComponentes solo con
  pccomtracker, mejor precio entre tiendas y bajada medida contra él, lecturas
  antiguas, reposición en cualquier tienda y la primera pasada sin avisos
  falsos.
- **Configuración y avisos:** actualización de la configuración antigua,
  formato de Telegram y Discord, foto rechazada, el 429 de Discord y migración
  idempotente.

Para pasadas reales sin enviar ni guardar nada:

```bash
docker compose exec silla-sihoo-bot python3 -m vigilante seguimiento --prueba
docker compose exec silla-sihoo-bot python3 -m vigilante descubrimiento --prueba
```

## Recursos y coste

- **Coste:** 0 €. Sin suscripciones, créditos ni APIs de pago.
- **Lectura de fichas:** cada una tarda 10-30 s y usa unos 300-500 MB de RAM
  mientras dura. Con 1 producto cada 2 h y hasta 20 verificaciones al día,
  son unos minutos de trabajo al día.
- **CholloMazon:** un CSV de unos 40 KB por hora.
- **Disco:** SQLite crece unos pocos MB al año.

## Desplegar en la Raspberry Pi

```bash
cd ~/homelab/bots/silla-ergonomica && git pull && docker compose up -d --build
docker compose logs -f --tail 50
```

## Volver a la versión anterior

```bash
cd ~/homelab/bots/silla-ergonomica && git checkout <commit> && docker compose up -d --build
```

Sustituye `<commit>` por el que quieras: `git log --oneline` muestra la lista.
La copia `data/config.json.v1.bak` es la configuración de la versión anterior.

## Historia

Cómo se llegó hasta aquí: el CAPTCHA de Amazon, el 429 de Sihoo, los
mensajes agrupados… Está en [docs/historia.md](docs/historia.md).
