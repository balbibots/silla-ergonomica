# Imagen oficial de Playwright para Python: trae Python y un Chromium ya
# instalado, y es multi-arquitectura (la misma etiqueta sirve en la ARM64 de
# una Raspberry Pi 5 que en un PC). Se usa para leer Amazon con un navegador
# real, porque con peticiones HTTP simples Amazon sirve un CAPTCHA casi siempre
# (ver README, "Lección aprendida: Amazon y el CAPTCHA").
#
# OJO: la version de la etiqueta (v1.63.0) tiene que coincidir con la del
# paquete Python de Playwright que trae la imagen; ya vienen emparejados, asi
# que si algun dia la cambias, cambia solo el numero de la etiqueta.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app

# Sihoo solo usa la libreria estandar; Playwright ya viene en la imagen.
COPY check_price.py loop_runner.py ./

# Para que los mensajes salgan al instante en "docker compose logs".
ENV PYTHONUNBUFFERED=1

# Nota: se ejecuta como root dentro del contenedor (simplificacion a
# proposito). Playwright ya lanza Chromium sin su sandbox interno por defecto,
# asi que no hacen falta permisos especiales. Para un bot personal que solo lee
# webs y escribe su propio archivo de estado, el riesgo es minimo, y evita los
# tipicos problemas de permisos de Docker con el volumen de state.json.

CMD ["python3", "loop_runner.py"]
