"""Ventana principal para configurar API y escanear arbitraje triangular USDT en Binance."""

from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from binance_arbitrage import BinanceArbitrageScanner

CONFIG_PATH = Path(".binance_gui_config.json")


class ArbitrageApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("HFTCryptoArbitrage - Binance USDT Triangular")
        self.root.geometry("1050x620")

        self.scanner = BinanceArbitrageScanner()
        self.running = False

        self.api_key_var = tk.StringVar()
        self.api_secret_var = tk.StringVar()
        self.usdt_var = tk.StringVar(value="100")
        self.fee_var = tk.StringVar(value="0.001")
        self.max_assets_var = tk.StringVar(value="120")
        self.status_var = tk.StringVar(value="Listo")

        self._build_ui()
        self._load_config()

    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        config_frame = ttk.Frame(notebook)
        scan_frame = ttk.Frame(notebook)
        notebook.add(config_frame, text="Configuración")
        notebook.add(scan_frame, text="Scanner Arbitraje")

        self._build_config_tab(config_frame)
        self._build_scan_tab(scan_frame)

    def _build_config_tab(self, frame: ttk.Frame):
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Binance API Key").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        ttk.Entry(frame, textvariable=self.api_key_var).grid(row=0, column=1, sticky="ew", padx=8, pady=8)

        ttk.Label(frame, text="Binance API Secret").grid(row=1, column=0, sticky="w", padx=8, pady=8)
        ttk.Entry(frame, textvariable=self.api_secret_var, show="*").grid(
            row=1, column=1, sticky="ew", padx=8, pady=8
        )

        ttk.Label(frame, text="Capital base USDT").grid(row=2, column=0, sticky="w", padx=8, pady=8)
        ttk.Entry(frame, textvariable=self.usdt_var).grid(row=2, column=1, sticky="ew", padx=8, pady=8)

        ttk.Label(frame, text="Fee por trade (ej. 0.001 = 0.1%)").grid(
            row=3, column=0, sticky="w", padx=8, pady=8
        )
        ttk.Entry(frame, textvariable=self.fee_var).grid(row=3, column=1, sticky="ew", padx=8, pady=8)

        ttk.Label(frame, text="Máx activos desde USDT").grid(row=4, column=0, sticky="w", padx=8, pady=8)
        ttk.Entry(frame, textvariable=self.max_assets_var).grid(row=4, column=1, sticky="ew", padx=8, pady=8)

        button_row = ttk.Frame(frame)
        button_row.grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=10)
        ttk.Button(button_row, text="Guardar configuración", command=self._save_config).pack(side="left", padx=4)
        ttk.Button(button_row, text="Validar conectividad", command=self._validate_keys).pack(side="left", padx=4)

        ttk.Label(frame, textvariable=self.status_var, foreground="blue").grid(
            row=6, column=0, columnspan=2, sticky="w", padx=8, pady=8
        )

        ttk.Label(
            frame,
            text=(
                "Nota: No se puede garantizar rentabilidad permanente. El scanner estima oportunidades "
                "antes de slippage/latencia real."
            ),
            foreground="darkred",
            wraplength=900,
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=8, pady=12)

    def _build_scan_tab(self, frame: ttk.Frame):
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Button(top, text="Escanear ahora", command=self._scan_once).pack(side="left", padx=4)
        ttk.Button(top, text="Auto-scan ON", command=self._start_autoscan).pack(side="left", padx=4)
        ttk.Button(top, text="Auto-scan OFF", command=self._stop_autoscan).pack(side="left", padx=4)

        cols = ("path", "symbols", "sides", "final_usdt", "profit_pct")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=20)
        for col, w in [("path", 260), ("symbols", 250), ("sides", 160), ("final_usdt", 130), ("profit_pct", 120)]:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

    def _save_config(self):
        payload = {
            "api_key": self.api_key_var.get().strip(),
            "api_secret": self.api_secret_var.get().strip(),
            "usdt": self.usdt_var.get().strip(),
            "fee": self.fee_var.get().strip(),
            "max_assets": self.max_assets_var.get().strip(),
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
            self.usdt_var.set(payload.get("usdt", "100"))
            self.fee_var.set(payload.get("fee", "0.001"))
            self.max_assets_var.set(payload.get("max_assets", "120"))
            self.status_var.set("Configuración cargada")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"No se pudo cargar configuración: {exc}")

    def _validate_keys(self):
        ok, msg = self.scanner.validate_api_keys(self.api_key_var.get().strip(), self.api_secret_var.get().strip())
        self.status_var.set(msg)
        if ok:
            messagebox.showinfo("Validación", msg)
        else:
            messagebox.showwarning("Validación", msg)

    def _scan_once(self):
        thread = threading.Thread(target=self._scan_worker, daemon=True)
        thread.start()

    def _scan_worker(self):
        try:
            start_usdt = float(self.usdt_var.get())
            fee = float(self.fee_var.get())
            max_assets = int(self.max_assets_var.get())

            self.scanner.fee_rate = fee
            rows = self.scanner.scan(start_usdt=start_usdt, max_paths=30, max_assets=max_assets)
            self.root.after(0, lambda: self._render_rows(rows))
            self.root.after(0, lambda: self.status_var.set(f"Scan completado: {len(rows)} rutas"))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: self.status_var.set(f"Error en scan: {exc}"))

    def _render_rows(self, rows):
        for item in self.tree.get_children():
            self.tree.delete(item)

        for row in rows:
            self.tree.insert(
                "",
                "end",
                values=(
                    " -> ".join(row.path),
                    " / ".join(row.symbols),
                    " / ".join(row.sides),
                    f"{row.final_usdt:.6f}",
                    f"{row.profit_pct:.4f}%",
                ),
            )

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
    app = ArbitrageApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
