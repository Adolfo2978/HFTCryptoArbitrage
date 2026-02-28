# HFTCryptoArbitrage

Sistema unificado en una sola ventana principal (`main.py`) con enfoque **100% operativo en Binance**.

## Qué incluye el sistema unificado

- Scanner de arbitraje triangular Binance (`USDT -> A -> B -> USDT`).
- Cálculo de ganancia limpia neta (incluye comisiones por ciclo).
- Simulación de interés compuesto con trigger al duplicar capital.
- Señales IA en Binance y flujo unificado en la misma ventana.

## Ventana principal (`main.py`)

Tienes 4 pestañas integradas:

1. **Configuración**
   - API keys, mercado, red, fee, filtros de ganancia limpia.
2. **Scanner**
   - Escaneo y ranking de rutas rentables netas.
3. **Estadística / interés compuesto**
   - KPIs, historial y simulación compuesta.
4. **Ejecución unificada Binance (AI + Scanner)**
   - IA de señal sobre Binance.
   - Cruce automático con scanner limpio para decidir si existe ruta favorable.

## Regla de rentabilidad limpia

Cada ciclo calcula:
- final bruto,
- comisiones totales,
- final neto,
- ganancia neta limpia.

Solo se muestran rutas con ganancia neta limpia superior al mínimo configurado.

## Regla de interés compuesto solicitada

- Capital inicial (ej. 10 USDT).
- Hasta duplicar capital (10 -> 20), fase pre-trigger.
- Al duplicar, se activa compuesto usando 10% del capital por ciclo (configurable).

## Auditoría de archivos

Se añadió `BINANCE_FILE_AUDIT.md` con la verificación archivo por archivo y el estado de migración al flujo Binance.

## Ejecutar

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

## Nota

El modelo es neto de comisiones, pero en real pueden afectar slippage, latencia y liquidez del libro.
