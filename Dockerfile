# Imagen oficial de Python, multi-arquitectura (funciona igual en la ARM64
# de una Raspberry Pi 5 que en un PC normal). No hace falta instalar nada
# con pip: el bot solo usa la libreria estandar de Python.
FROM python:3.12-slim

WORKDIR /app

COPY check_price.py loop_runner.py ./

# Nota: se ejecuta como root dentro del contenedor (simplificacion a
# proposito). Para un bot personal que solo lee webs y escribe un archivo
# de estado propio, el riesgo es minimo, y evita los tipicos problemas de
# permisos de Docker con el volumen de state.json al empezar con Docker.

CMD ["python", "loop_runner.py"]
