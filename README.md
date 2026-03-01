# HFTCryptoArbitrage

## Sistema completo en **un solo archivo**

Todo el sistema principal está en **`HFTCryptoArbitrage.py`**.

### Pestañas incluidas en el GUI principal

1. **⚙️ Configuración**
   - API keys, mercado, red, fee y filtros de ganancia limpia.
   - Validación de conectividad.
2. **🔍 Scanner**
   - Escaneo y ranking de rutas rentables netas.
   - KPI cards, filtro en tiempo real y resaltado de mejor oportunidad.
3. **📈 Estadísticas**
   - Historial de escaneos.
   - Simulación de interés compuesto.
   - Gráfico ASCII simple de evolución.
4. **🚀 Ejecución Unificada**
   - IA de señal sobre Binance.
   - Cruce automático con scanner limpio.
   - Registro visible de traders ejecutados.

### Mejoras UX/robustez

- Barra de menú superior (Archivo / Scanner / Ayuda) para una navegación más profesional.
- Header limpio (sin saturación de botones) con selector de tema.
- Tema claro/oscuro configurable.
- Toolbar superior y status bar inferior fija.
- Indicador visual de operación en curso + progressbar.
- Logs por nivel (INFO/WARN/ERROR).
- Atajos: `Ctrl+S` guardar, `F5` escanear, `Esc` detener autoscan.
- Confirmación para operación crítica en modo operativo.

## Ejecutar

```bash
python3 HFTCryptoArbitrage.py
```

> `main.py` se mantiene solo como entrada de compatibilidad.
