# HFTCryptoArbitrage

Aplicación para análisis de arbitraje triangular en Binance con **base USDT** y ciclo completo **USDT -> A -> B -> USDT**, incluyendo ventana principal para configuración de API y escaneo en tiempo real.

## ¿Qué se mejoró?

- Se añadió una **ventana principal (`main.py`)** con menú/pestañas para:
  - introducir `API Key` y `API Secret`,
  - configurar capital base USDT,
  - configurar fee por trade,
  - lanzar escaneo manual o automático.
- Se añadió un motor robusto de arbitraje en `binance_arbitrage.py` que:
  - descarga mercados spot de Binance,
  - construye rutas triangulares válidas,
  - calcula retorno estimado por ruta usando `bid/ask` + fee,
  - prioriza oportunidades por `% de beneficio`.
- Se mantuvo enfoque de seguridad: no hay ejecución automática de órdenes en vivo.

## Importante sobre rentabilidad

No es posible garantizar que "todo sea rentable" de forma permanente en arbitraje real.
Este sistema muestra **rentabilidad estimada** antes de:
- slippage,
- latencia de red,
- límites de tamaño mínimo,
- cambios rápidos del order book.

Aun así, el sistema está diseñado para ayudarte a detectar oportunidades de forma más eficiente.

## Instalación

```bash
python3 -m pip install -r requirements.txt
```

## Ejecutar la ventana principal

```bash
python3 main.py
```

## Ejecutar scanner por script (sin GUI)

```bash
python3 binance_arbitrage.py
```

## Dependencias

- `requests` para Binance API pública.
- `numpy/pandas/pybit` se mantienen para módulos previos del repo.
