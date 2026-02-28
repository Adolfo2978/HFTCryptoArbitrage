# Auditoría de archivos del proyecto (enfoque Binance)

Esta auditoría verifica el estado de cada archivo del repositorio y su alineación con un sistema unificado para Binance.

## Archivos Python

- `main.py` ✅ **Mejorado**
  - Centro de control unificado.
  - Scanner Binance + estadísticas + interés compuesto + ejecución unificada Binance (AI + scanner).
- `binance_arbitrage.py` ✅ **Mejorado**
  - Cálculo de ganancia neta limpia (fees incluidas), filtro por rentabilidad mínima y métricas.
- `ai_signal_system.py` ✅ **Mejorado**
  - IA de señales orientada a Binance por defecto (`exchange='binance'`) y compatibilidad opcional con Bybit.
- `Test_bybit.py` ⚠️ **Legacy/compatibilidad**
  - Se conserva para flujos Bybit existentes, pero el flujo principal recomendado está en Binance desde `main.py`.

## Documentación y dependencias

- `README.md` ✅ **Actualizado**
  - Describe el sistema unificado en una sola ventana principal.
- `requirements.txt` ✅ **Vigente**
  - Dependencias declaradas para IA, data fetch y conectividad API.

## Notebooks heredados (2 años)

Estos archivos se marcan como **históricos** y no forman parte del flujo productivo unificado Binance:

- `Binance.ipynb`
- `BitMex.ipynb`
- `Bybit.ipynb`
- `BybitV2.ipynb`
- `GetOpenOrdersBybit.ipynb`
- `Python_Websocket.ipynb`
- `SendOrder.ipynb`
- `TCP_Ping.ipynb`
- `Trading.ipynb`
- `pip install bitmex-websocket.ipynb`

## Recomendación final

Para operación en producción:
1. Usar sólo `main.py`, `binance_arbitrage.py`, `ai_signal_system.py`, `README.md`, `requirements.txt`.
2. Mantener notebooks como archivo histórico o moverlos a carpeta `legacy/` en una fase siguiente.
