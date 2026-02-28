# HFTCryptoArbitrage

Plataforma de arbitraje triangular Binance con GUI avanzada y enfoque en **ganancia limpia neta**.

## Qué hace ahora (según tu regla)

- Analiza ciclos `USDT -> A -> B -> USDT`.
- Calcula por cada ciclo:
  - final bruto (sin comisiones),
  - comisiones totales del ciclo,
  - final neto,
  - ganancia neta limpia en USDT y en %.
- Filtra y muestra solo rutas con **ganancia limpia > mínimo** configurable.

## Soporte de mercado/red

- Spot + Perpetuals (USDT-M).
- Mainnet + Testnet.

## Interés compuesto (regla solicitada)

Se añadió simulación con tu lógica:

1. Capital inicial (ejemplo: 10 USDT).
2. Hasta duplicar el capital (10 -> 20), fase pre-trigger.
3. Cuando llega al doble, se activa compuesto con **10% del capital por ciclo** (configurable).
4. El crecimiento se aplica con la rentabilidad neta limpia estimada por ciclo.

## GUI (`main.py`)

Incluye:
- configuración API,
- selector mercado/red,
- fee y mínimo de ganancia limpia,
- KPI en tiempo real,
- tabla con detalle de fees/gross/net,
- historial de escaneos,
- panel de detalle con simulación de interés compuesto.

## Ejecución

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

## Nota realista

El sistema garantiza cálculo neto de comisiones dentro del modelo, pero en real pueden afectar:
- slippage,
- latencia,
- profundidad de libro,
- fills parciales.
