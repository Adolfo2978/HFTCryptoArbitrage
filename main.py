"""GUI principal: Binance Spot/Perpetual (Mainnet/Testnet) + ganancia limpia y plan compuesto."""

from __future__ import annotations

import json
import threading
import tkinter as tk
from datetime import UTC, datetime
from pathlib import Path
from tkinter import messagebox, ttk

from binance_arbitrage import BinanceArbitrageScanner, ScanOutput

CONFIG_PATH = Path(".binance_gui_config.json")


class ArbitrageApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("HFTCryptoArbitrage - Control Center")
        self.root.geometry("1360x820")

        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.scanner = BinanceArbitrageScanner()
        self.running = False
        self.scan_history = []
        self.last_output: ScanOutput | None = None

        self.api_key_var = tk.StringVar()
        self.api_secret_var = tk.StringVar()
        self.market_var = tk.StringVar(value="spot")
        self.network_var = tk.StringVar(value="testnet")
        self.usdt_var = tk.StringVar(value="10")
        self.fee_var = tk.StringVar(value="0.001")
        self.max_assets_var = tk.StringVar(value="120")
        self.min_clean_profit_var = tk.StringVar(value="0.0001")

        self.compound_cycles_var = tk.StringVar(value="50")
        self.compound_trigger_multiple_var = tk.StringVar(value="2.0")
        self.compound_stake_pct_var = tk.StringVar(value="0.10")

        self.status_var = tk.StringVar(value="Listo")
        self.kpi_scan_ms = tk.StringVar(value="0 ms")
        self.kpi_success = tk.StringVar(value="0.00%")
        self.kpi_best = tk.StringVar(value="0.0000%")
        self.kpi_avg = tk.StringVar(value="0.0000%")

        self._build_ui()
        self._load_config()

    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        config_frame = ttk.Frame(notebook, padding=10)
        scan_frame = ttk.Frame(notebook, padding=10)
        details_frame = ttk.Frame(notebook, padding=10)
        notebook.add(config_frame, text="Configuración")
        notebook.add(scan_frame, text="Scanner")
        notebook.add(details_frame, text="Estadística / interés compuesto")

        self._build_config_tab(config_frame)
        self._build_scan_tab(scan_frame)
        self._build_details_tab(details_frame)

    def _build_config_tab(self, frame: ttk.Frame):
        frame.columnconfigure(1, weight=1)

        rows = [
            ("Binance API Key", self.api_key_var),
            ("Binance API Secret", self.api_secret_var),
            ("Capital inicial (USDT)", self.usdt_var),
            ("Fee por trade (0.001 = 0.1%)", self.fee_var),
            ("Máx activos USDT", self.max_assets_var),
            ("Ganancia limpia mínima por ciclo (USDT)", self.min_clean_profit_var),
            ("Ciclos simulados interés compuesto", self.compound_cycles_var),
            ("Trigger compuesto (x capital inicial)", self.compound_trigger_multiple_var),
            ("Stake compuesto post-trigger (0.10 = 10%)", self.compound_stake_pct_var),
        ]

        for idx, (label, var) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=idx, column=0, sticky="w", pady=5)
            show = "*" if "Secret" in label else None
            ttk.Entry(frame, textvariable=var, show=show).grid(row=idx, column=1, sticky="ew", pady=5)

        ttk.Label(frame, text="Mercado").grid(row=9, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=self.market_var, values=["spot", "perpetual"], state="readonly").grid(
            row=9, column=1, sticky="ew", pady=5
        )

        ttk.Label(frame, text="Red").grid(row=10, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=self.network_var, values=["mainnet", "testnet"], state="readonly").grid(
            row=10, column=1, sticky="ew", pady=5
        )

        actions = ttk.Frame(frame)
        actions.grid(row=11, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Button(actions, text="Guardar configuración", command=self._save_config).pack(side="left", padx=4)
        ttk.Button(actions, text="Validar conectividad API", command=self._validate_keys).pack(side="left", padx=4)

        ttk.Label(frame, textvariable=self.status_var, foreground="#0047ab").grid(row=12, column=0, columnspan=2, sticky="w")

        ttk.Label(
            frame,
            text=(
                "Se muestran SOLO rutas con ganancia limpia (neta de comisiones) > mínimo definido. "
                "Interés compuesto: se activa al duplicar capital y usa 10% (configurable) por ciclo."
            ),
            wraplength=1100,
            foreground="#8b0000",
        ).grid(row=13, column=0, columnspan=2, sticky="w", pady=10)

    def _build_scan_tab(self, frame: ttk.Frame):
        top = ttk.Frame(frame)
        top.pack(fill="x")

        ttk.Button(top, text="Escanear ahora", command=self._scan_once).pack(side="left", padx=4)
        ttk.Button(top, text="Auto-scan ON", command=self._start_autoscan).pack(side="left", padx=4)
        ttk.Button(top, text="Auto-scan OFF", command=self._stop_autoscan).pack(side="left", padx=4)

        kpis = ttk.Frame(frame)
        kpis.pack(fill="x", pady=8)
        self._kpi_card(kpis, "Tiempo scan", self.kpi_scan_ms).pack(side="left", padx=4)
        self._kpi_card(kpis, "Tasa éxito limpia", self.kpi_success).pack(side="left", padx=4)
        self._kpi_card(kpis, "Mejor net %", self.kpi_best).pack(side="left", padx=4)
        self._kpi_card(kpis, "Promedio %", self.kpi_avg).pack(side="left", padx=4)

        cols = ("rank", "path", "symbols", "net_profit", "fees", "gross_final", "net_final", "profit_pct")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=19)
        widths = {
            "rank": 55,
            "path": 210,
            "symbols": 230,
            "net_profit": 110,
            "fees": 100,
            "gross_final": 110,
            "net_final": 110,
            "profit_pct": 100,
        }
        for col in cols:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=widths[col], anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)

    def _build_details_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text="Historial de escaneos", font=("Arial", 11, "bold")).grid(row=0, column=0, sticky="w")
        hist_cols = ("timestamp", "market", "network", "valid", "clean", "success", "best", "ms")
        self.hist_tree = ttk.Treeview(frame, columns=hist_cols, show="headings", height=8)
        widths = {"timestamp": 155, "market": 90, "network": 90, "valid": 80, "clean": 80, "success": 95, "best": 95, "ms": 80}
        for col in hist_cols:
            self.hist_tree.heading(col, text=col)
            self.hist_tree.column(col, width=widths[col], anchor="center")
        self.hist_tree.grid(row=1, column=0, sticky="nsew")

        ttk.Label(frame, text="Detalle trader + simulación interés compuesto", font=("Arial", 11, "bold")).grid(
            row=2, column=0, sticky="w", pady=(10, 4)
        )
        self.detail_text = tk.Text(frame, height=12, wrap="word")
        self.detail_text.grid(row=3, column=0, sticky="nsew")

    def _kpi_card(self, parent, title: str, value_var: tk.StringVar):
        card = ttk.Frame(parent, padding=8)
        ttk.Label(card, text=title).pack(anchor="w")
        ttk.Label(card, textvariable=value_var, font=("Arial", 13, "bold")).pack(anchor="w")
        return card

    def _save_config(self):
        payload = {
            "api_key": self.api_key_var.get().strip(),
            "api_secret": self.api_secret_var.get().strip(),
            "market": self.market_var.get().strip(),
            "network": self.network_var.get().strip(),
            "usdt": self.usdt_var.get().strip(),
            "fee": self.fee_var.get().strip(),
            "max_assets": self.max_assets_var.get().strip(),
            "min_clean_profit": self.min_clean_profit_var.get().strip(),
            "compound_cycles": self.compound_cycles_var.get().strip(),
            "compound_trigger_multiple": self.compound_trigger_multiple_var.get().strip(),
            "compound_stake_pct": self.compound_stake_pct_var.get().strip(),
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.status_var.set("Configuración guardada")

    def _load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.api_key_var.set(payload.get("api_key", ""))
            self.api_secret_var.set(payload.get("api_secret", ""))
            self.market_var.set(payload.get("market", "spot"))
            self.network_var.set(payload.get("network", "testnet"))
            self.usdt_var.set(payload.get("usdt", "10"))
            self.fee_var.set(payload.get("fee", "0.001"))
            self.max_assets_var.set(payload.get("max_assets", "120"))
            self.min_clean_profit_var.set(payload.get("min_clean_profit", "0.0001"))
            self.compound_cycles_var.set(payload.get("compound_cycles", "50"))
            self.compound_trigger_multiple_var.set(payload.get("compound_trigger_multiple", "2.0"))
            self.compound_stake_pct_var.set(payload.get("compound_stake_pct", "0.10"))
            self.status_var.set("Configuración cargada")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"No se pudo cargar configuración: {exc}")

    def _configure_scanner(self):
        self.scanner.configure(
            market_type=self.market_var.get().strip(),
            testnet=self.network_var.get().strip() == "testnet",
            fee_rate=float(self.fee_var.get()),
        )

    def _validate_keys(self):
        try:
            self._configure_scanner()
            ok, msg = self.scanner.validate_api_keys(self.api_key_var.get().strip(), self.api_secret_var.get().strip())
            self.status_var.set(msg)
            if ok:
                messagebox.showinfo("Validación", msg)
            else:
                messagebox.showwarning("Validación", msg)
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"Error validando: {exc}")

    def _scan_once(self):
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            self._configure_scanner()
            start_usdt = float(self.usdt_var.get())
            max_assets = int(self.max_assets_var.get())
            min_clean_profit = float(self.min_clean_profit_var.get())

            output = self.scanner.scan(
                start_usdt=start_usdt,
                max_paths=40,
                max_assets=max_assets,
                min_clean_profit_usdt=min_clean_profit,
            )
            self.root.after(0, lambda: self._render_output(output))
            self.root.after(0, lambda: self.status_var.set(f"Scan limpio completado: {len(output.opportunities)} rutas"))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: self.status_var.set(f"Error en scan: {exc}"))

    def _render_output(self, output: ScanOutput):
        self.last_output = output
        for item in self.tree.get_children():
            self.tree.delete(item)

        for idx, row in enumerate(output.opportunities, start=1):
            self.tree.insert(
                "",
                "end",
                values=(
                    idx,
                    " -> ".join(row.path),
                    " / ".join(row.symbols),
                    f"{row.net_profit_usdt:.6f}",
                    f"{row.total_fees_usdt:.6f}",
                    f"{row.gross_final_usdt:.6f}",
                    f"{row.final_usdt:.6f}",
                    f"{row.profit_pct:.4f}%",
                ),
            )

        self.kpi_scan_ms.set(f"{output.stats.scan_ms} ms")
        self.kpi_success.set(f"{output.stats.success_rate_pct:.2f}%")
        self.kpi_best.set(f"{output.stats.best_profit_pct:.4f}%")
        self.kpi_avg.set(f"{output.stats.avg_profit_pct:.4f}%")
        self._append_history(output)

    def _append_history(self, output: ScanOutput):
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        row = (
            ts,
            output.stats.market_type,
            "testnet" if output.stats.testnet else "mainnet",
            output.stats.valid_paths,
            output.stats.clean_profitable_paths,
            f"{output.stats.success_rate_pct:.2f}%",
            f"{output.stats.best_profit_pct:.4f}%",
            output.stats.scan_ms,
        )
        self.scan_history.append(row)
        self.scan_history = self.scan_history[-25:]

        for item in self.hist_tree.get_children():
            self.hist_tree.delete(item)
        for item in self.scan_history:
            self.hist_tree.insert("", "end", values=item)

    def _on_row_select(self, _event):
        sel = self.tree.selection()
        if not sel or not self.last_output:
            return
        values = self.tree.item(sel[0], "values")
        if not values:
            return

        idx = int(values[0]) - 1
        if idx < 0 or idx >= len(self.last_output.opportunities):
            return

        row = self.last_output.opportunities[idx]
        initial_capital = float(self.usdt_var.get())
        cycles = int(self.compound_cycles_var.get())
        trigger_multiple = float(self.compound_trigger_multiple_var.get())
        compound_stake_pct = float(self.compound_stake_pct_var.get())

        compound = self.scanner.simulate_compound_plan(
            initial_capital_usdt=initial_capital,
            per_cycle_net_pct=row.profit_pct,
            cycles=cycles,
            trigger_multiple=trigger_multiple,
            compound_stake_pct=compound_stake_pct,
            pre_trigger_stake_pct=1.0,
        )

        text = (
            f"Ruta: {' -> '.join(row.path)}\n"
            f"Símbolos: {' / '.join(row.symbols)}\n"
            f"Lados: {' / '.join(row.sides)}\n\n"
            f"Resultado ciclo (LIMPIO):\n"
            f"- Final bruto (sin fee): {row.gross_final_usdt:.6f} USDT\n"
            f"- Comisiones totales ciclo: {row.total_fees_usdt:.6f} USDT\n"
            f"- Final neto (con fee): {row.final_usdt:.6f} USDT\n"
            f"- Ganancia neta limpia: {row.net_profit_usdt:.6f} USDT ({row.profit_pct:.4f}%)\n\n"
            f"Interés compuesto simulado:\n"
            f"- Capital inicial: {compound.initial_capital:.4f} USDT\n"
            f"- Capital final: {compound.current_capital:.4f} USDT\n"
            f"- Ciclos: {compound.cycles_simulated}\n"
            f"- Trigger alcanzado: {compound.trigger_reached}\n"
            f"- Ciclo de trigger: {compound.trigger_cycle}\n"
            f"- Modo stake: {compound.stake_mode}\n"
            f"- Net % por ciclo asumido: {compound.per_cycle_net_pct:.4f}%\n\n"
            "Regla aplicada:\n"
            "1) Hasta duplicar capital, se usa 100% del capital por ciclo (según ejemplo).\n"
            "2) Tras duplicar (ej. 10 -> 20), se activa compuesto con 10% del capital/ciclo.\n"
            "3) Sólo se listan ciclos con ganancia neta limpia > mínimo configurado.\n"
        )
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", text)

    def _autoscan_loop(self):
        if not self.running:
            return
        self._scan_once()
        self.root.after(5000, self._autoscan_loop)

    def _start_autoscan(self):
        if self.running:
            return
        self.running = True
        self.status_var.set("Auto-scan activado (cada 5s)")
        self._autoscan_loop()

    def _stop_autoscan(self):
        self.running = False
        self.status_var.set("Auto-scan detenido")


def main():
    root = tk.Tk()
    ArbitrageApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
