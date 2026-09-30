#!/usr/bin/env python3
"""
Punto de entrada para correr el bot dentro de un contenedor Docker de forma
permanente, en vez de que algo externo (cron, GitHub Actions) lo lance cada
cierto tiempo.

No toca nada de check_price.py: simplemente llama a su main() una vez, y
luego se queda dormido hasta la siguiente vuelta. Si una vuelta concreta
falla por una excepcion que check_price.py no haya sabido manejar (no
deberia pasar, pero por si acaso), lo registramos y seguimos con la
siguiente vuelta en vez de morir el contenedor entero.

Variable de entorno:
  CHECK_INTERVAL_HOURS   cada cuantas horas se comprueba (por defecto 6)
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import check_price

INTERVALO_HORAS = float(os.environ.get("CHECK_INTERVAL_HOURS", "6"))


def main() -> None:
    print("Bot arrancado en modo bucle. Comprobando cada %.1f horas." % INTERVALO_HORAS,
          flush=True)
    while True:
        ahora = datetime.now(timezone.utc).isoformat()
        print("\n[%s] Nueva comprobacion..." % ahora, flush=True)
        try:
            check_price.main()
        except Exception as exc:  # noqa: BLE001
            # check_price.main() ya gestiona sus propios errores de red/parseo
            # internamente; esto solo cubre un fallo realmente inesperado (un
            # bug, no una web caida), para que el contenedor no se muera.
            print("ERROR INESPERADO en esta vuelta: %s" % exc, flush=True)

        print("Esperando %.1f horas hasta la siguiente comprobacion..." % INTERVALO_HORAS,
              flush=True)
        time.sleep(INTERVALO_HORAS * 3600)


if __name__ == "__main__":
    sys.exit(main())
