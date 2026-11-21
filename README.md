# Vigilante de precios

Bot personal que corre en Docker en una Raspberry Pi 5 y avisa por Telegram
(o Discord) cuando pasa algo relevante con un precio. Coste de funcionamiento:
**0 €**. Tiene dos modos independientes:

1. **Seguimiento:** los productos que tú eliges, por ASIN o URL de Amazon
   España.
2. **Descubrimiento:** ofertas nuevas por categorías.

El bot trabaja en silencio. No manda mensajes de "comprobación hecha", de
precio sin cambios, de reinicio ni de errores puntuales: todo eso va a los
logs y a la base de datos.

## Estado (noviembre de 2026)

| Función | Estado |
|---|---|
| Seguimiento de varios productos (añadir y quitar por ASIN o URL) | ✅ Operativo |
| Nuevo, segunda mano, reacondicionado y agotado bien distinguidos | ✅ Operativo |
| Avisos de bajada, precio objetivo, mínimo registrado y reposición | ✅ Operativo |
| Tarjetas con foto, precio destacado, referencia tachada y botón | ✅ Telegram. ⏳ Discord: hecho y probado en simulación, falta un webhook real |
| Histórico en SQLite, migración del CSV y CSV compatible | ✅ Operativo |
| Mínimo de 365 días con el gráfico de Amazon | 🟡 Importación manual: el bot no puede consultarlo solo (ver abajo) |
| Comprobación cada 15 minutos | ❌ Ninguna fuente lo autoriza. Sigue cada 12 h, configurable |
| Descubrimiento por categorías | ⏳ Lógica, filtros, límites y pruebas listos. **Sin fuente real**: viene desactivado |

## Fuentes y autorización

Lo que se investigó y comprobó el 21/11/2026 antes de elegir:

| Fuente | Qué da | ¿Se puede usar? | Prueba |
|---|---|---|---|
| **Ficha de Amazon** (`/dp/ASIN`) | Precio actual, condición, vendedor, imagen y valoraciones | Sí, como hasta ahora: un Chromium real, sin sesión, una ficha cada 12 h por producto | Las [Condiciones de Uso](https://www.amazon.es/gp/help/customer/display.html?nodeId=201909000) prohíben la "extracción sistemática" de "partes sustanciales" sin permiso escrito. Una ficha cada 12 h para uso propio no busca eso, pero **Amazon no autoriza expresamente ningún acceso automatizado**. Por eso no se sube a cada 15 min ni se recorren categorías así |
| **Historial de 365 días de Amazon** | Precio más bajo de cada día, sin envío | Solo a mano | Llega por `POST /rufus/cl/streaming`, el canal interno del asistente de compras, con tu **sesión iniciada** (cookies `at-acbes`, `sess-at-acbes`…) y un token anti-CSRF. No es una API pública, y automatizarlo supondría reutilizar tu sesión |
| **Amazon Creators API** | Precio, condición, disponibilidad, vendedor, imágenes y búsqueda por categoría | Sí, **si eres elegible** | [Requisitos](https://affiliate-program.amazon.com/creatorsapi/docs/): cuenta de afiliado aceptada y **10 ventas cualificadas en 30 días**. Gratis. **No da histórico** |
| **CamelCamelCamel** | Histórico, mayores bajadas y RSS | No | Sus [condiciones](https://es.camelcamelcamel.com/tos): "Scraping and/or other automated data collection from our website and/or emails is prohibited", sin excepción para el RSS |
| **Keepa API** | Histórico completo | No | Es de pago (descartado) |
| **Histórico propio** | Lo que el bot ha leído desde que empezó | Sí | No dice nada de antes de la primera lectura |

Conclusión: no hay hoy **ninguna fuente gratuita y autorizada** que dé
candidatos por categoría con su histórico. El descubrimiento queda listo para
cuando la haya (por ejemplo, la Creators API si llegas a ser elegible) y,
mientras, no finge que funciona.

## Configuración

`data/config.json` se crea solo la primera vez, copiando
[`config.example.json`](config.example.json). Se puede editar sin tocar
código; el bot lo relee en cada vuelta y lo valida entero al arrancar.

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| `canal` | `telegram` | `telegram`, `discord` o `consola` (solo escribe en el log) |
| `seguimiento.intervalo_min_por_fuente` | `amazon_ficha: 720` | Minutos entre lecturas de cada producto |
| `seguimiento.reglas.bajada_pct` / `bajada_eur` | `5` / `10` | Bajada que merece aviso: 5 % **o** 10 € frente a la última lectura válida |
| `seguimiento.reglas.min_observaciones_minimo` / `min_dias_minimo` | `10` / `14` | Historial que hace falta para hablar de "mínimo registrado" |
| `seguimiento.reglas.historial_caducidad_dias` | `30` | Días que vale un historial de 365 días importado |
| `seguimiento.avisos_tecnicos` | `false` | Avisar si un producto falla `fallos_antes_de_aviso_tecnico` veces seguidas |
| `productos[]` | Xiaomi G20 | `asin`, `nombre`, `categoria`, `precio_objetivo` y, opcionalmente, `bajada_pct` y `bajada_eur` propios |
| `descubrimiento.*` | desactivado | Categorías, filtros y límites del modo 2 (ver más abajo) |

Los secretos van en `.env` ([`.env.example`](.env.example)), nunca en el
JSON.

### Añadir y quitar productos

```bash
docker compose exec silla-sihoo-bot python3 -m vigilante anadir "https://www.amazon.es/dp/B0DJ82JKFL" --nombre "Xiaomi G20" --categoria Aspiradoras --objetivo 125
docker compose exec silla-sihoo-bot python3 -m vigilante quitar B0DJ82JKFL
docker compose exec silla-sihoo-bot python3 -m vigilante listar
docker compose exec silla-sihoo-bot python3 -m vigilante estado
```

Se valida el ASIN (solo `amazon.es`) y no se admiten duplicados. Al quitar un
producto, su histórico se conserva.

## Qué avisa

Un aviso por acontecimiento. Si coinciden varios, sale uno solo con el motivo
principal y los demás como notas:

| Prioridad | Aviso | Cuándo |
|---|---|---|
| 1 | 🏆 Nuevo mínimo de 365 días | Por debajo del mínimo del gráfico de 365 días importado, más lo leído desde entonces |
| 2 | 🏆 Nuevo mínimo registrado | Por debajo del menor precio que el bot ha visto, con historial suficiente |
| 3 | 🎯 Precio objetivo alcanzado | El precio a pagar (con cupón, si lo hay) llega a tu objetivo. Se rearma al superarlo |
| 4 | 📉 Bajada de precio | Al menos 5 % o 10 € frente a la última lectura válida |
| 5 | 🔁 Vuelve a su mínimo registrado | Solo después de haber subido |
| 6 | 📦 De nuevo en stock | Vuelve a haber unidades **nuevas** tras no haberlas |

No avisa:

- de que se agota (se registra y se espera a la reposición);
- de las ofertas de segunda mano o reacondicionadas (se guardan aparte);
- de una primera lectura como "récord";
- del mismo evento al mismo precio dentro de 24 h.

Un aviso que no se puede enviar queda pendiente y se reintenta en la
siguiente vuelta.

**Cómo se calcula:**

- **Precios:** todo en céntimos. Las series comparan el mismo ASIN, la
  condición "nuevo" y el precio de la oferta destacada sin cupón.
- **Media de 90 días:** la media de las medias diarias, y solo si hay lecturas
  en al menos el 90 % de los días. Si no, no aparece.
- **Mínimo de 365 días:** el gráfico de Amazon muestra el precio más bajo de
  cualquier oferta de cada día. Eso nunca es mayor que el de la oferta
  destacada, así que si el precio queda por debajo es un mínimo seguro. No se
  usa si la importación ha caducado o si el bot ha dejado más de 2 días sin
  leer desde entonces.

### Formato

```text
🏆 NUEVO MÍNIMO DE 365 DÍAS
Xiaomi Vacuum Cleaner G20

💶 129,99 € | Media 90 d: 199,99 € (tachado)
📉 Ahorro: 70,00 € (35 %)

📊 Mínimo anterior (365 d): 139,99 €
🏷 Categoría: Aspiradoras
✅ En stock · Nuevo, vendido por Amazon
📅 Comprobado: 21/11/2026 16:30
Fuente: ficha de Amazon e historial de 365 d de Amazon (importado el 21/11/2026) · Envío no incluido

[🛒 Ver en Amazon]
```

(Datos ficticios de ejemplo.)

- **Telegram:** `sendPhoto` con la foto del producto, el texto con negrita y
  tachado, y un botón real «🛒 Ver en Amazon».
  - Si Telegram rechaza la foto, manda el mismo texto con `sendMessage`.
  - Si el error es de red, no lo reenvía, para no duplicarlo.
  - El texto se recorta para no pasar de 1024 caracteres.
- **Discord:** un webhook con embed: borde de color según el aviso, título
  enlazado a Amazon, miniatura y pie con fuente y condiciones. Las menciones
  están desactivadas y se respeta el 429.
  - Un webhook normal no admite botones interactivos. Discord indica que
    puede enviar componentes no interactivos, pero no dice expresamente que
    los botones de enlace lo sean. Para no depender de eso, el enlace va en el
    título y como «🛒 Ver en Amazon» en el texto.

**Recomendación: seguir con Telegram.** Ya funciona y tiene foto grande y
botón real. Discord queda listo como alternativa. Para probarlo:

1. En el canal privado de Discord: Editar canal → Integraciones → Webhooks →
   Nuevo webhook → Copiar URL.
2. Pon la URL en `.env` como `DISCORD_WEBHOOK_URL=...`.
3. Cambia `"canal": "discord"` en `data/config.json`.

Si el canal elegido no está configurado, el bot no arranca con él: nunca
cambia de canal solo ni envía a los dos.

## Historial de 365 días (importación manual)

1. En tu navegador, abre la ficha del producto, pulsa **Historial de precios**
   y espera a que salga el gráfico.
2. En DevTools → Network → «Save all as HAR».
3. Cópialo a la Pi e impórtalo:

   ```bash
   cp ~/Descargas/www.amazon.es.har ~/homelab/bots/silla-ergonomica/data/historial.har
   docker compose exec silla-sihoo-bot python3 -m vigilante importar-historial data/historial.har --asin B0DJ82JKFL
   rm ~/homelab/bots/silla-ergonomica/data/historial.har
   ```

Del HAR solo se lee el JSON del gráfico; las cookies y las cabeceras se
ignoran y no se guardan. Aun así, **borra el HAR después**: contiene tu
sesión de Amazon.

El gráfico trae unos 95 puntos en un año, no uno por día, así que con él no
se calculan medias, solo el mínimo y el máximo. Si el producto solo tiene
ofertas de segunda mano, Amazon no muestra histórico de nuevo.

## Descubrimiento de ofertas (modo 2)

Pide candidatos a un **proveedor** por categoría (como máximo
`max_candidatos` por ejecución), los filtra y verifica su histórico. Solo
avisa de los mejores:

- Precio entre 25 y 600 €.
- Valoración de 4 o más, con al menos 25 valoraciones.
- Solo productos nuevos y disponibles.
- Marcas, palabras clave y palabras excluidas configurables.
- Al menos 90 días de histórico.
- Descuento de al menos un 20 % **y** 10 € frente a la media verificada de
  90 días.
- El precio debe estar en el mínimo del periodo o por debajo.
- Ordenados por ahorro ponderado por la valoración.
- Como máximo 5 avisos por ejecución y 15 al día. Nunca dos veces el mismo
  producto al mismo precio.
- Lo que no se notifica se guarda en la tabla `candidatos`.
- Se ejecuta aparte del seguimiento (cada `intervalo_min`, dentro de
  `horario`). Si falla, el seguimiento sigue.

Hay dos modos de mínimo:

- `externo`: el histórico del proveedor, y solo si es diario.
- `local`: lo que el bot ha visto, siempre con la etiqueta "observado por el
  bot desde…".

Proveedores:

- `simulado`: lee un JSON, para pruebas.
- `ninguno`: el valor por defecto.

Para activarlo hace falta un proveedor real autorizado (ver "Fuentes y
autorización") y escribir su clase con los métodos `candidatos(categoria,
limite)` e `historial(asin)`. Los identificadores de categoría deben salir del
catálogo del propio proveedor, no inventarse.

## Datos

En `data/`, en la propia Pi:

- **`precios.db` (SQLite):** observaciones, fallos aparte (nunca pisan el
  último precio bueno), estado de cada producto, avisos enviados, historiales
  importados y candidatos.
- **`historico_precios.csv`:** se sigue rellenando con las mismas columnas de
  siempre.
- **Migración:** la primera vez, el CSV antiguo pasa a SQLite. Antes se copia
  a `data/copias/historico_precios.csv.antes-de-migrar`. Se puede repetir sin
  duplicar nada (`python3 -m vigilante migrar` muestra el recuento).
- **Exportación completa:** `python3 -m vigilante exportar-csv data/exportacion.csv`.

## Pruebas

```bash
python -m unittest discover -s tests -v
```

Son 44 pruebas sin red. Cubren:

- **Lectura de la ficha:** producto y variante correctos, precio de oferta
  frente al tachado y las cuotas, decimales con coma, página sin precio,
  CAPTCHA con HTTP 200, cupones, y nuevo frente a usado aunque la ficha diga
  "En stock".
- **Reglas de aviso:**
  - mínimos nuevos e igualados, y la falta de historial;
  - historial de 365 días caducado o con huecos, y su caché;
  - un solo aviso cuando coinciden varios, y sin duplicados;
  - ciclos sin cambios sin avisos, y persistencia tras reiniciar;
  - avisos pendientes.
- **Avisos y datos:** formato de Telegram y de Discord, foto rechazada,
  errores dudosos sin duplicar, el 429 de Discord, migración idempotente,
  configuración y límites del descubrimiento.

Para una pasada real sin enviar ni guardar nada:

```bash
docker compose exec silla-sihoo-bot python3 -m vigilante seguimiento --prueba
```

## Recursos y coste

- **Coste:** 0 €. Sin suscripciones, créditos ni APIs de pago.
- **Lectura:** abrir una ficha con Chromium tarda 10-30 s y usa unos
  300-500 MB de RAM mientras dura. Con 1 producto cada 12 h, la Pi está
  ociosa casi todo el tiempo. Entre fichas de Amazon hay 60 s de pausa.
- **Disco:** SQLite crece unos pocos KB al mes por producto.

## Desplegar en la Raspberry Pi

```bash
cd ~/homelab/bots/silla-ergonomica && git pull && docker compose up -d --build
docker compose logs -f --tail 50
```

En el primer arranque:

- crea `data/config.json` y `data/precios.db`;
- migra el CSV y el `state.json`;
- lee la G20 en la primera vuelta.

Mientras solo haya oferta de segunda mano, el log dirá `sin unidades nuevas`
y no llegará nada.

## Volver a la versión anterior

```bash
cd ~/homelab/bots/silla-ergonomica && git checkout d0b68a9 && docker compose up -d --build
```

La versión anterior sigue usando `state.json` y el CSV, que no se han tocado.
Para volver a la actual: `git checkout main && docker compose up -d --build`.

## Historia

Cómo se llegó hasta aquí: el CAPTCHA de Amazon, el 429 de Sihoo, los
mensajes agrupados… Está en [docs/historia.md](docs/historia.md).
