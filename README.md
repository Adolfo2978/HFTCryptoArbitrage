# HFTCryptoArbitrage

Sistema unificado en una sola ventana principal (`main.py`) para:

- Scanner de arbitraje triangular Binance (`USDT -> A -> B -> USDT`).
- Cálculo de ganancia limpia neta (incluye comisiones por ciclo).
- Simulación de interés compuesto con trigger al duplicar capital.
- Señales IA para Bybit y ejecución opcional desde la misma GUI.

## Todo unificado en la ventana principal

En `main.py` ahora tienes 4 pestañas integradas:

1. **Configuración**
   - API keys, mercado, red, fee, filtros de ganancia limpia.
2. **Scanner**
   - Escaneo y ranking de rutas rentables netas.
3. **Estadística / interés compuesto**
   - KPIs, historial y simulación compuesta.
4. **Ejecución unificada (AI + Bybit)**
   - Análisis IA de señal.
   - Flujo completo IA -> decisión -> ejecución opcional (dry-run o real).

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

## Ejecutar

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

## Nota

El modelo es neto de comisiones, pero en real pueden afectar slippage, latencia y liquidez del libro.
