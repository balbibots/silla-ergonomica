#!/usr/bin/env python3
"""Compatibilidad: el contenedor arranca ahora con "python3 -m vigilante bucle".

Se conserva este archivo por si algo lo sigue llamando. Para volver al bot
antiguo (check_price.py) basta con desplegar el commit anterior (ver README,
"Volver a la version anterior").
"""
import sys

from vigilante.__main__ import main

if __name__ == "__main__":
    sys.exit(main(["bucle"]))
