# HFTCryptoArbitrage

Sistema unificado en una sola ventana principal (`main.py`) con enfoque operativo en Binance.

## 4 pestañas integradas en un único GUI principal

1. **Configuración**
   - API keys, mercado, red, fee, filtros de ganancia limpia.
2. **Scanner**
   - Escaneo y ranking de rutas rentables netas.
3. **Estadística / interés compuesto**
   - KPIs, historial y simulación compuesta.
4. **Ejecución unificada Binance (AI + Scanner)**
   - IA de señal sobre Binance.
   - Cruce automático con scanner limpio para decidir si existe ruta favorable.
   - Registro visible de **traders ejecutados** en tabla (timestamp, símbolo, señal, ruta, net profit, modo, estado).

## Mejoras de robustez y estilo profesional

- Diseño de GUI con estilo consistente (`ttk`, cards KPI, tipografía homogénea).
- Validaciones de entrada numérica para evitar errores silenciosos.
- Registro en vivo de ejecución y trazabilidad operativa por ciclo.
- Persistencia de configuración para reinicio rápido.

## Producción (recomendación)

- Ejecutar en **testnet** primero.
- Ajustar fees reales de cuenta.
- Confirmar latencia y calidad de datos.
- Activar modo operativo solo tras validación.

## Ejecutar

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

## Auditoría del proyecto

`BINANCE_FILE_AUDIT.md` resume estado archivo por archivo y componentes legacy.
