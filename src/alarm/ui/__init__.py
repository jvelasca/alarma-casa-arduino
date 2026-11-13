"""Interfaz de escritorio.

Este paquete **no** se importa desde la logica del dispositivo ni desde la
consola de linea de comandos: solo `alarm.ui.app` necesita PyQt6. Mantener el
`__init__` vacio permite importar `alarm.ui.audio` desde un script sin arrastrar
el toolkit grafico.
"""
