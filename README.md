# HFTCryptoArbitrage

Plataforma de arbitraje triangular en Binance con GUI mejorada, soporte de **Spot/Perpetuals** y selección de **Mainnet/Testnet**.

## Novedades principales

- Ventana principal (`main.py`) con entorno gráfico ampliado:
  - configuración de API Key/Secret,
  - selector de mercado (`spot` o `perpetual`),
  - selector de red (`mainnet` o `testnet`),
  - escaneo manual y automático,
  - panel KPI en tiempo real (ms de scan, tasa de éxito, mejor profit, profit promedio),
  - historial de escaneos,
  - detalle trader para cada ruta seleccionada.

- Motor de arbitraje (`binance_arbitrage.py`) mejorado:
  - soporte API Spot y Futures USDT-M Perpetual,
  - endpoints productivos y testnet,
  - evaluación de rutas `USDT -> A -> B -> USDT`,
  - estadísticas completas por escaneo:
    - rutas analizadas,
    - rutas rentables,
    - tasa de éxito,
    - mejor/avg/mediana de profit,
    - tiempo total de ejecución.

## Importante sobre “éxito completo”

El sistema entrega **éxito analítico y operativo** (escaneo robusto + métricas + detalle),
pero no puede garantizar rentabilidad constante en mercado real por:
- slippage,
- latencia,
- cambios del libro,
- límites de tamaño y comisiones dinámicas.

## Instalación

```bash
python3 -m pip install -r requirements.txt
```

## Ejecutar GUI

```bash
python3 main.py
```

## Ejecutar scanner por script

```bash
python3 binance_arbitrage.py
```

## Recomendación de uso

1. Comenzar en **testnet**.
2. Ajustar fee real de tu cuenta.
3. Revisar KPI de tasa de éxito y profit medio.
4. Pasar a mainnet sólo tras validación de latencia/ejecución.
