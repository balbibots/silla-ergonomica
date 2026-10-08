# Imagen oficial de Playwright: Ubuntu 24.04 con Python y los navegadores
# (Chromium incluido) ya descargados en /ms-playwright. Es multi-arquitectura:
# la misma etiqueta sirve en la ARM64 de una Raspberry Pi 5 que en un PC. Se usa
# para leer Amazon con un navegador real, porque con peticiones HTTP simples
# Amazon sirve un CAPTCHA casi siempre (ver README, "Lección aprendida: Amazon
# y el CAPTCHA").
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app

# OJO (aprendido a la mala, 2026-10-08): pese al nombre "python", esta imagen
# trae los NAVEGADORES pero NO el paquete de Python de Playwright (lo instala
# en un entorno temporal solo para descargar los navegadores y luego lo borra).
# Hay que instalarlo aqui, y con EXACTAMENTE la misma version que la etiqueta
# de la imagen (v1.63.0), porque cada version de Playwright espera su propia
# revision de Chromium. Si algun dia cambias la etiqueta, cambia tambien este
# numero.
RUN pip install --no-cache-dir playwright==1.63.0 \
    && python3 -c "from importlib.metadata import version; import playwright.sync_api; print('Playwright', version('playwright'), 'importable')"

# Sihoo solo usa la libreria estandar.
COPY check_price.py loop_runner.py ./

# Para que los mensajes salgan al instante en "docker compose logs".
ENV PYTHONUNBUFFERED=1

# En Docker Amazon se lee SIEMPRE con el navegador. Con "auto", si Playwright
# faltara por cualquier motivo el bot caeria en silencio al modo HTTP (que sufre
# CAPTCHA casi siempre) y el fallo pasaria desapercibido; fijandolo aqui, un
# Playwright roto da un error claro en el aviso de Telegram.
ENV AMAZON_MODO=navegador

# Nota: se ejecuta como root dentro del contenedor (simplificacion a
# proposito). Playwright ya lanza Chromium sin su sandbox interno por defecto,
# asi que no hacen falta permisos especiales. Para un bot personal que solo lee
# webs y escribe su propio archivo de estado, el riesgo es minimo, y evita los
# tipicos problemas de permisos de Docker con el volumen de state.json.

CMD ["python3", "loop_runner.py"]
