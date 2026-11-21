"""Vigilante de precios: seguimiento de productos y descubrimiento de ofertas.

Modulos:
  modelo          datos comunes (observaciones, precios en centimos, fechas)
  config          configuracion JSON validada al arrancar
  almacen         SQLite: observaciones, fallos, estado, avisos, historicos
  amazon          lectura e interpretacion de la ficha de Amazon
  historial_amazon  importacion manual del grafico de 365 dias desde un HAR
  reglas          que observacion merece un aviso (y cual, si hay varias)
  tarjeta         el aviso como tarjeta visual, independiente del canal
  notificadores   Telegram, Discord y consola (modo prueba)
  seguimiento     modo 1: productos que sigues por ASIN o URL
  descubrimiento  modo 2: ofertas por categorias (necesita un proveedor)
"""
