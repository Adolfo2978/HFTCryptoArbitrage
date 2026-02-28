# Auditoría integral de archivos (estado actual Binance)

## Estado general

- ✅ Flujo principal unificado en `main.py`.
- ✅ Scanner y métricas de arbitraje en `binance_arbitrage.py`.
- ✅ Señal IA con soporte Binance en `ai_signal_system.py`.
- ⚠️ `Test_bybit.py` permanece para compatibilidad legacy.

## Verificación archivo por archivo

### Núcleo productivo

- `main.py` ✅
  - 4 pestañas en un solo GUI.
  - vista de traders ejecutados.
  - validaciones de entrada.
  - panel de ejecución unificada Binance.
- `binance_arbitrage.py` ✅
  - rutas triangulares USDT.
  - profit neto y fees.
  - estadísticas de escaneo y simulación compuesta.
- `ai_signal_system.py` ✅
  - IA de señal configurable por exchange.
  - Binance como ruta principal recomendada.
- `README.md` ✅
  - documentación alineada con GUI unificado y operación Binance.
- `requirements.txt` ✅
  - dependencias declaradas.

### Compatibilidad / histórico

- `Test_bybit.py` ⚠️
  - útil para legado Bybit, no es flujo principal Binance.
- Notebooks `.ipynb` ⚠️
  - históricos, no forman parte del runtime productivo del GUI.

## Conclusión

El proyecto queda organizado para operación en Binance desde un único GUI principal, con visibilidad de ejecución, robustez de validación y trazabilidad operativa.
