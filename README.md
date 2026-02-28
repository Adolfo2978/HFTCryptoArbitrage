# HFTCryptoArbitrage

## Sistema completo en **un solo archivo**

Ahora todo el sistema está incluido en **`main.py`** y cumple todas las funciones en un único GUI principal:

1. **Configuración**
   - API keys, mercado, red, fee, filtros de ganancia limpia.
2. **Scanner**
   - Escaneo y ranking de rutas rentables netas.
3. **Estadística / interés compuesto**
   - KPIs, historial y simulación compuesta.
4. **Ejecución unificada Binance (AI + Scanner)**
   - IA de señal sobre Binance.
   - Cruce automático con scanner limpio para decidir si existe ruta favorable.
   - Tabla visible de **traders ejecutados**.

## Incluido dentro de `main.py`

- Scanner triangular Binance (Spot/Perpetual, Mainnet/Testnet).
- Cálculo neto limpio con fees por ciclo.
- Simulación de interés compuesto con trigger.
- Motor IA ligero para señal Binance/Bybit.
- GUI profesional y robusta con validaciones.

## Ejecución

```bash
python3 main.py
```

> Si quieres usar módulos legacy (`ai_signal_system.py`, `binance_arbitrage.py`, `Test_bybit.py`) se mantienen por compatibilidad, pero el flujo principal de producción queda unificado en `main.py`.
