# HFTCryptoArbitrage

Sistema de análisis y trading cripto orientado a alta frecuencia, ahora mejorado con un motor de señales basado en IA ligero y eficiente.

## Mejoras implementadas

1. **Seguridad operativa**
   - Se eliminaron credenciales hardcodeadas.
   - El trading real requiere variables de entorno (`BYBIT_API_KEY`, `BYBIT_API_SECRET`).
   - El script de ejecución está en **modo dry-run por defecto**.

2. **Predicción de señales con IA**
   - Nuevo módulo `ai_signal_system.py` que:
     - Descarga velas de Bybit (endpoint público).
     - Construye features técnicas eficientes (`retornos`, `EMA gap`, `z-score de volumen`, `volatilidad`).
     - Entrena un modelo de **regresión logística online con SGD** (rápido y de baja latencia).
     - Emite señal: `BUY`, `SELL` o `HOLD` según probabilidad y umbrales.

3. **Eficiencia y mantenimiento**
   - Pipeline modular (`cliente de mercado`, `features`, `modelo`, `motor de señal`).
   - Reintentos y timeout para robustez de datos.
   - Validación out-of-sample simple para seguimiento de accuracy.

## Uso rápido

### 1) Solo predicción IA (sin credenciales)

```bash
python3 ai_signal_system.py --symbol BTCUSDT --interval 1 --limit 500
```

### 2) Trading asistido por IA (dry-run)

```bash
python3 Test_bybit.py --symbol BTCUSDT --qty 0.001
```

### 3) Trading real (bajo tu responsabilidad)

```bash
export BYBIT_API_KEY="tu_api_key"
export BYBIT_API_SECRET="tu_api_secret"
python3 Test_bybit.py --symbol BTCUSDT --qty 0.001 --execute
```

## Advertencia

Este repositorio es experimental. No es asesoría financiera. Usa gestión de riesgo, límites de exposición, y pruebas en testnet antes de operar en real.
