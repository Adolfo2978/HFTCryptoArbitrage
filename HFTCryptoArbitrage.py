"""HFTCryptoArbitrage - single-file unified system with enhanced professional GUI."""

from __future__ import annotations

import json
import math
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CONFIG_PATH = Path(".binance_gui_config.json")


# ==========================
# DATA MODELS
# ==========================

@dataclass(frozen=True)
class Edge:
    from_asset: str
    to_asset: str
    symbol: str
    side: str


@dataclass
class ArbitrageResult:
    path: Tuple[str, str, str, str]
    symbols: Tuple[str, str, str]
    sides: Tuple[str, str, str]
    gross_final_usdt: float
    final_usdt: float
    total_fees_usdt: float
    net_profit_usdt: float
    profit_pct: float
    is_clean_profitable: bool


@dataclass
class ScanStats:
    market_type: str
    testnet: bool
    scanned_paths: int
    valid_paths: int
    clean_profitable_paths: int
    success_rate_pct: float
    best_profit_pct: float
    avg_profit_pct: float
    median_profit_pct: float
    scan_ms: int


@dataclass
class ScanOutput:
    opportunities: List[ArbitrageResult]
    stats: ScanStats


@dataclass
class CompoundPlanResult:
    initial_capital: float
    current_capital: float
    cycles_simulated: int
    trigger_reached: bool
    trigger_cycle: int
    stake_mode: str
    per_cycle_net_pct: float


# ==========================
# UTILS
# ==========================


def http_get_json(url: str, timeout: int = 10) -> dict | list:
    req = Request(url, headers={"User-Agent": "HFTCryptoArbitrage/2.0"})
    try:
        with urlopen(req, timeout=timeout) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP error {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


class ToolTip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None):
        if self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + 20
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(self.tip, text=self.text, background="#1f2937", foreground="white", relief="solid", borderwidth=1, padx=6, pady=4)
        label.pack()

    def _hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ==========================
# BINANCE SCANNER
# ==========================


class BinanceArbitrageScanner:
    def __init__(self, fee_rate: float = 0.001, timeout: int = 10, market_type: str = "spot", testnet: bool = False):
        self.fee_rate = fee_rate
        self.timeout = timeout
        self.market_type = market_type.lower()
        self.testnet = testnet

    def configure(self, market_type: str, testnet: bool, fee_rate: float):
        self.market_type = market_type.lower()
        self.testnet = testnet
        self.fee_rate = fee_rate

    def _api_base_url(self) -> str:
        if self.market_type == "spot":
            return "https://testnet.binance.vision/" if self.testnet else "https://api.binance.com"
        if self.market_type == "perpetual":
            return "https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"
        raise ValueError("market_type debe ser 'spot' o 'perpetual'")

    def _endpoint(self, key: str) -> str:
        if self.market_type == "spot":
            m = {"exchange_info": "/api/v3/exchangeInfo", "book_ticker": "/api/v3/ticker/bookTicker", "time": "/api/v3/time"}
        else:
            m = {"exchange_info": "/fapi/v1/exchangeInfo", "book_ticker": "/fapi/v1/ticker/bookTicker", "time": "/fapi/v1/time"}
        return m[key]

    def _get(self, endpoint: str, params: Optional[dict] = None):
        query = f"?{urlencode(params)}" if params else ""
        return http_get_json(f"{self._api_base_url().rstrip('/')}{endpoint}{query}", timeout=self.timeout)

    def fetch_exchange_info(self) -> List[dict]:
        payload = self._get(self._endpoint("exchange_info"))
        symbols = payload.get("symbols", [])
        if self.market_type == "spot":
            return [s for s in symbols if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)]
        return [s for s in symbols if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL" and s.get("quoteAsset")]

    def fetch_book_tickers(self) -> Dict[str, Tuple[float, float]]:
        payload = self._get(self._endpoint("book_ticker"))
        prices: Dict[str, Tuple[float, float]] = {}
        for row in payload:
            bid = float(row["bidPrice"])
            ask = float(row["askPrice"])
            if bid > 0 and ask > 0:
                prices[row["symbol"]] = (bid, ask)
        return prices

    @staticmethod
    def _build_edges(symbol_rows: List[dict]) -> Dict[str, Dict[str, Edge]]:
        graph: Dict[str, Dict[str, Edge]] = {}
        for row in symbol_rows:
            symbol = row.get("symbol")
            base = row.get("baseAsset")
            quote = row.get("quoteAsset")
            if not symbol or not base or not quote:
                continue
            graph.setdefault(quote, {})[base] = Edge(quote, base, symbol, "BUY")
            graph.setdefault(base, {})[quote] = Edge(base, quote, symbol, "SELL")
        return graph

    @staticmethod
    def _convert_once(amount: float, edge: Edge, prices: Dict[str, Tuple[float, float]]) -> Optional[float]:
        book = prices.get(edge.symbol)
        if not book:
            return None
        bid, ask = book
        if edge.side == "BUY":
            return amount / ask if ask > 0 else None
        return amount * bid if bid > 0 else None

    def _convert_with_fee(self, amount: float, edge: Edge, prices: Dict[str, Tuple[float, float]]) -> Optional[float]:
        out = self._convert_once(amount, edge, prices)
        if out is None:
            return None
        return out * (1 - self.fee_rate)

    def _enumerate_triangles(self, graph: Dict[str, Dict[str, Edge]], max_assets: int = 120):
        neighbors = list(graph.get("USDT", {}).keys())[:max_assets]
        for a in neighbors:
            e1 = graph["USDT"].get(a)
            if not e1:
                continue
            for b, e2 in graph.get(a, {}).items():
                if b == "USDT":
                    continue
                e3 = graph.get(b, {}).get("USDT")
                if e2 and e3:
                    yield ("USDT", a, b, "USDT"), (e1, e2, e3)

    def _eval_cycle(self, start_usdt: float, edges: Tuple[Edge, Edge, Edge], prices: Dict[str, Tuple[float, float]]):
        gross = start_usdt
        net = start_usdt
        for edge in edges:
            gross = self._convert_once(gross, edge, prices)
            net = self._convert_with_fee(net, edge, prices)
            if gross is None or net is None:
                return None
        fees = max(0.0, gross - net)
        net_profit = net - start_usdt
        profit_pct = (net_profit / start_usdt) * 100 if start_usdt > 0 else 0.0
        return gross, net, fees, net_profit, profit_pct

    def scan(self, start_usdt: float, max_paths: int = 40, max_assets: int = 120, min_clean_profit_usdt: float = 0.0) -> ScanOutput:
        t0 = time.time()
        symbols = self.fetch_exchange_info()
        prices = self.fetch_book_tickers()
        graph = self._build_edges(symbols)

        all_rows: List[ArbitrageResult] = []
        scanned = 0
        for path, edges in self._enumerate_triangles(graph, max_assets=max_assets):
            scanned += 1
            cycle = self._eval_cycle(start_usdt, edges, prices)
            if cycle is None:
                continue
            gross, net, fees, net_profit, profit_pct = cycle
            all_rows.append(
                ArbitrageResult(
                    path=path,
                    symbols=(edges[0].symbol, edges[1].symbol, edges[2].symbol),
                    sides=(edges[0].side, edges[1].side, edges[2].side),
                    gross_final_usdt=gross,
                    final_usdt=net,
                    total_fees_usdt=fees,
                    net_profit_usdt=net_profit,
                    profit_pct=profit_pct,
                    is_clean_profitable=net_profit > min_clean_profit_usdt,
                )
            )

        clean = [r for r in all_rows if r.is_clean_profitable]
        clean.sort(key=lambda x: x.profit_pct, reverse=True)
        profits = [r.profit_pct for r in all_rows]
        median = 0.0
        if profits:
            s = sorted(profits)
            median = s[len(s) // 2]

        stats = ScanStats(
            market_type=self.market_type,
            testnet=self.testnet,
            scanned_paths=scanned,
            valid_paths=len(all_rows),
            clean_profitable_paths=len(clean),
            success_rate_pct=(len(clean) / len(all_rows) * 100.0) if all_rows else 0.0,
            best_profit_pct=max(profits) if profits else 0.0,
            avg_profit_pct=(sum(profits) / len(profits)) if profits else 0.0,
            median_profit_pct=median,
            scan_ms=int((time.time() - t0) * 1000),
        )
        return ScanOutput(opportunities=clean[:max_paths], stats=stats)

    @staticmethod
    def simulate_compound_plan(initial_capital_usdt: float, per_cycle_net_pct: float, cycles: int, trigger_multiple: float = 2.0, compound_stake_pct: float = 0.10, pre_trigger_stake_pct: float = 1.0) -> CompoundPlanResult:
        capital = initial_capital_usdt
        trigger = initial_capital_usdt * trigger_multiple
        reached = False
        trigger_cycle = -1

        for i in range(1, cycles + 1):
            if capital >= trigger:
                if not reached:
                    reached = True
                    trigger_cycle = i
                stake_pct = compound_stake_pct
            else:
                stake_pct = pre_trigger_stake_pct
            capital += (capital * stake_pct) * (per_cycle_net_pct / 100.0)

        return CompoundPlanResult(
            initial_capital=initial_capital_usdt,
            current_capital=capital,
            cycles_simulated=cycles,
            trigger_reached=reached,
            trigger_cycle=trigger_cycle,
            stake_mode=f"pre:{pre_trigger_stake_pct*100:.1f}% | post:{compound_stake_pct*100:.1f}%",
            per_cycle_net_pct=per_cycle_net_pct,
        )

    def validate_api_keys(self, api_key: str, api_secret: str) -> Tuple[bool, str]:
        if not api_key or not api_secret:
            return False, "Faltan API key/secret"
        if len(api_key) < 20 or len(api_secret) < 20:
            return False, "Formato de API key/secret parece inválido"
        try:
            _ = self._get(self._endpoint("time"))
            env = "TESTNET" if self.testnet else "MAINNET"
            return True, f"Conectividad {env} OK para {self.market_type.upper()}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Error conectividad Binance: {exc}"


# ==========================
# LIGHTWEIGHT AI SIGNAL
# ==========================


class AISignalEngine:
    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def _fetch_binance(self, symbol: str, interval: str, limit: int) -> List[list]:
        q = urlencode({"symbol": symbol, "interval": interval, "limit": limit})
        rows = http_get_json(f"https://api.binance.com/api/v3/klines?{q}", timeout=self.timeout)
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Sin klines Binance")
        return rows

    def _fetch_bybit(self, symbol: str, interval: str, limit: int) -> List[list]:
        q = urlencode({"category": "linear", "symbol": symbol, "interval": interval, "limit": limit})
        payload = http_get_json(f"https://api.bybit.com/v5/market/kline?{q}", timeout=self.timeout)
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            raise RuntimeError("Error Bybit klines")
        rows = payload.get("result", {}).get("list", [])
        if not rows:
            raise RuntimeError("Sin klines Bybit")
        return sorted(rows, key=lambda x: int(x[0]))

    def run(self, symbol: str, interval: str, limit: int, exchange: str = "binance") -> Dict[str, float | str]:
        ex = exchange.lower().strip()
        if ex == "binance":
            rows = self._fetch_binance(symbol, interval, limit)
            closes = [float(r[4]) for r in rows]
            ts = int(rows[-1][0])
        elif ex == "bybit":
            rows = self._fetch_bybit(symbol, interval, limit)
            closes = [float(r[4]) for r in rows]
            ts = int(rows[-1][0])
        else:
            raise ValueError("exchange debe ser 'binance' o 'bybit'")

        if len(closes) < 40:
            raise RuntimeError("Datos insuficientes para IA")

        short = sum(closes[-8:]) / 8
        long_ = sum(closes[-21:]) / 21
        momentum = (short / long_) - 1.0 if long_ else 0.0

        rets = []
        for i in range(1, min(30, len(closes))):
            prev = closes[-i - 1]
            cur = closes[-i]
            if prev > 0:
                rets.append((cur / prev) - 1.0)

        vol = math.sqrt(sum(r * r for r in rets) / len(rets)) if rets else 0.0
        score = momentum * 120 - vol * 8
        prob_up = 1.0 / (1.0 + math.exp(-max(-8.0, min(8.0, score))))
        signal = "BUY" if prob_up >= 0.58 else "SELL" if prob_up <= 0.42 else "HOLD"

        return {
            "exchange": ex,
            "symbol": symbol,
            "interval": interval,
            "rows": len(closes),
            "last_close": closes[-1],
            "last_candle_utc": datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).isoformat(),
            "test_accuracy": round(max(0.5, 1.0 - vol * 30), 4),
            "probability_up": round(prob_up, 4),
            "signal": signal,
        }


# ==========================
# GUI APP
# ==========================


class ArbitrageApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("🚀 HFTCryptoArbitrage | Sistema Unificado")
        self.root.geometry("1600x900")
        self.root.minsize(1200, 700)

        self.theme = {
            "bg": "#f8f9fa", "fg": "#212529",
            "primary": "#0d6efd", "success": "#198754",
            "warning": "#ffc107", "danger": "#dc3545",
            "card_bg": "#ffffff", "border": "#dee2e6",
            "profit": "#198754", "loss": "#dc3545", "neutral": "#6c757d"
        }

        self.dark_mode = tk.BooleanVar(value=False)
        self.scanner = BinanceArbitrageScanner()
        self.ai_engine = AISignalEngine()

        self.running = False
        self.scan_history: List[tuple] = []
        self.executed_trades: List[tuple] = []
        self.last_output: Optional[ScanOutput] = None
        self.all_scan_rows: List[ArbitrageResult] = []
        self.toast_window: Optional[tk.Toplevel] = None
        self._pulse_on = False

        self._init_variables()

        self._setup_style()
        self._build_main_layout()
        self._setup_keyboard_shortcuts()
        self._load_config()
        self._update_status("Sistema listo • Conectado a Binance Testnet", "info")


    def _init_variables(self):
        """Inicializa variables de control con defaults robustos."""
        self.api_key_var = tk.StringVar()
        self.api_secret_var = tk.StringVar()
        self.market_var = tk.StringVar(value="spot")
        self.network_var = tk.StringVar(value="testnet")
        self.usdt_var = tk.StringVar(value="100.00")
        self.fee_var = tk.StringVar(value="0.001")
        self.max_assets_var = tk.StringVar(value="120")
        self.min_clean_profit_var = tk.StringVar(value="0.01")
        self.min_profit_var = self.min_clean_profit_var

        self.compound_cycles_var = tk.StringVar(value="50")
        self.compound_trigger_multiple_var = tk.StringVar(value="2.0")
        self.compound_stake_pct_var = tk.StringVar(value="0.10")

        self.binance_symbol_var = tk.StringVar(value="BTCUSDT")
        self.binance_qty_var = tk.StringVar(value="100.00")
        self.binance_interval_var = tk.StringVar(value="1m")
        self.binance_limit_var = tk.StringVar(value="300")
        self.binance_testnet_var = tk.BooleanVar(value=True)
        self.binance_execute_var = tk.BooleanVar(value=False)

        self.search_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Listo")
        self.status_type_var = tk.StringVar(value="info")
        self.kpi_scan_ms = tk.StringVar(value="0 ms")
        self.kpi_success = tk.StringVar(value="0.00%")
        self.kpi_best = tk.StringVar(value="0.0000%")
        self.kpi_avg = tk.StringVar(value="0.0000%")
        # Compatibilidad: algunos flujos esperan diccionario de KPIs
        self.kpi_vars = {
            "scan_time": self.kpi_scan_ms,
            "success_rate": self.kpi_success,
            "best_profit": self.kpi_best,
            "avg_profit": self.kpi_avg,
            "opportunities": tk.StringVar(value="0"),
        }

    def _build_main_layout(self):
        self._build_ui()

    def _setup_keyboard_shortcuts(self):
        self._bind_shortcuts()

    def _setup_style(self):
        self.style = ttk.Style()
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")
        self._apply_theme()

    def _apply_theme(self):
        dark = self.dark_mode.get()
        bg = "#111827" if dark else "#f3f4f6"
        fg = "#e5e7eb" if dark else "#111827"
        card = "#1f2937" if dark else "#ffffff"

        self.root.configure(bg=bg)
        self.style.configure("TFrame", background=bg)
        self.style.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        self.style.configure("TCheckbutton", background=bg, foreground=fg)
        self.style.configure("TNotebook", background=bg)
        self.style.configure("TNotebook.Tab", padding=(14, 8), font=("Segoe UI", 10, "bold"))
        self.style.configure("Card.TFrame", background=card, relief="solid", borderwidth=1)
        self.style.configure("CardTitle.TLabel", background=card, foreground=fg, font=("Segoe UI", 9))
        self.style.configure("CardValue.TLabel", background=card, foreground="#10b981" if dark else "#047857", font=("Segoe UI", 14, "bold"))
        self.style.configure("Status.TLabel", background=bg, foreground="#3b82f6", font=("Segoe UI", 10, "bold"))

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # Toolbar
        toolbar = ttk.Frame(self.root)
        toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        ttk.Button(toolbar, text="💾 Guardar", command=self._save_config).pack(side="left", padx=3)
        ttk.Button(toolbar, text="🔍 Escanear", command=self._scan_once).pack(side="left", padx=3)
        ttk.Button(toolbar, text="▶ Flujo Unificado", command=self._run_unified_flow).pack(side="left", padx=3)
        ttk.Button(toolbar, text="❌ Cancelar AutoScan", command=self._stop_autoscan).pack(side="left", padx=3)
        ttk.Checkbutton(toolbar, text="Tema oscuro", variable=self.dark_mode, command=self._apply_theme).pack(side="right", padx=4)

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)

        self.tab_config = ttk.Frame(notebook, padding=12)
        self.tab_scan = ttk.Frame(notebook, padding=12)
        self.tab_stats = ttk.Frame(notebook, padding=12)
        self.tab_exec = ttk.Frame(notebook, padding=12)

        notebook.add(self.tab_config, text="⚙️ Configuración")
        notebook.add(self.tab_scan, text="🔍 Scanner")
        notebook.add(self.tab_stats, text="📈 Estadísticas")
        notebook.add(self.tab_exec, text="🚀 Ejecución Unificada")

        self._build_tab_config()
        self._build_tab_scan()
        self._build_tab_stats()
        self._build_tab_exec()

        self._build_statusbar()

    def _build_statusbar(self):
        status = ttk.Frame(self.root)
        status.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 8))
        self.dot = tk.Label(status, text="●", fg="#6b7280")
        self.dot.pack(side="left", padx=(0, 6))
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        ttk.Label(status, text="v1.2.0 • Python 3.10+", font=("Segoe UI", 8), foreground=self.theme["neutral"]).pack(side="right")
        self._update_connection_indicator("disconnected")

    def _build_config_tab(self):
        self._build_tab_config()

    def _build_scanner_tab(self):
        self._build_tab_scan()

    def _build_stats_tab(self):
        self._build_tab_stats()

    def _build_execution_tab(self):
        self._build_tab_exec()

    def _build_tab_config(self):
        f = self.tab_config
        f.columnconfigure(1, weight=1)

        api = ttk.LabelFrame(f, text="API")
        api.grid(row=0, column=0, columnspan=2, sticky="ew", pady=6)
        api.columnconfigure(1, weight=1)
        ttk.Label(api, text="API Key").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        e_key = ttk.Entry(api, textvariable=self.api_key_var)
        e_key.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        ToolTip(e_key, "Clave API Binance")

        ttk.Label(api, text="API Secret").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        e_secret = ttk.Entry(api, textvariable=self.api_secret_var, show="*")
        e_secret.grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        ToolTip(e_secret, "Secreto API Binance")

        params = ttk.LabelFrame(f, text="Parámetros")
        params.grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)
        params.columnconfigure(1, weight=1)

        rows = [
            ("Capital inicial USDT", self.usdt_var),
            ("Fee por trade", self.fee_var),
            ("Max assets", self.max_assets_var),
            ("Ganancia limpia mínima", self.min_clean_profit_var),
        ]
        for i, (lbl, var) in enumerate(rows):
            ttk.Label(params, text=lbl).grid(row=i, column=0, sticky="w", padx=6, pady=4)
            ent = ttk.Entry(params, textvariable=var)
            ent.grid(row=i, column=1, sticky="ew", padx=6, pady=4)
            ToolTip(ent, f"Configurar {lbl}")

        ttk.Label(params, text="Mercado").grid(row=4, column=0, sticky="w", padx=6, pady=4)
        cb_market = ttk.Combobox(params, textvariable=self.market_var, values=["spot", "perpetual"], state="readonly")
        cb_market.grid(row=4, column=1, sticky="ew", padx=6, pady=4)

        ttk.Label(params, text="Red").grid(row=5, column=0, sticky="w", padx=6, pady=4)
        cb_network = ttk.Combobox(params, textvariable=self.network_var, values=["mainnet", "testnet"], state="readonly")
        cb_network.grid(row=5, column=1, sticky="ew", padx=6, pady=4)
        ToolTip(cb_network, "Usa testnet para pruebas")

        comp = ttk.LabelFrame(f, text="Interés compuesto")
        comp.grid(row=2, column=0, columnspan=2, sticky="ew", pady=6)
        comp.columnconfigure(1, weight=1)

        c_rows = [
            ("Ciclos", self.compound_cycles_var),
            ("Trigger x capital", self.compound_trigger_multiple_var),
            ("Stake post-trigger", self.compound_stake_pct_var),
        ]
        for i, (lbl, var) in enumerate(c_rows):
            ttk.Label(comp, text=lbl).grid(row=i, column=0, sticky="w", padx=6, pady=4)
            ttk.Entry(comp, textvariable=var).grid(row=i, column=1, sticky="ew", padx=6, pady=4)

        buttons = ttk.Frame(f)
        buttons.grid(row=3, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(buttons, text="Guardar configuración", command=self._save_config).pack(side="left", padx=4)
        ttk.Button(buttons, text="Validar conectividad API", command=self._validate_keys).pack(side="left", padx=4)

    def _build_tab_scan(self):
        f = self.tab_scan
        f.columnconfigure(0, weight=1)
        f.rowconfigure(3, weight=1)

        ctrl = ttk.Frame(f)
        ctrl.grid(row=0, column=0, sticky="ew")
        ttk.Button(ctrl, text="Escanear ahora", command=self._scan_once).pack(side="left", padx=4)
        ttk.Button(ctrl, text="Auto-scan ON", command=self._start_autoscan).pack(side="left", padx=4)
        ttk.Button(ctrl, text="Auto-scan OFF", command=self._stop_autoscan).pack(side="left", padx=4)

        ttk.Label(ctrl, text="Filtro rápido:").pack(side="left", padx=(20, 6))
        s_entry = ttk.Entry(ctrl, textvariable=self.search_var, width=24)
        s_entry.pack(side="left")
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        ToolTip(s_entry, "Filtra por símbolo o ruta")

        self.progress = ttk.Progressbar(f, mode="indeterminate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=4)

        kpis = ttk.Frame(f)
        kpis.grid(row=2, column=0, sticky="ew", pady=6)
        self._kpi_card(kpis, "Tiempo scan", self.kpi_scan_ms).pack(side="left", padx=4)
        self._kpi_card(kpis, "Tasa éxito", self.kpi_success).pack(side="left", padx=4)
        self._kpi_card(kpis, "Mejor %", self.kpi_best).pack(side="left", padx=4)
        self._kpi_card(kpis, "Promedio %", self.kpi_avg).pack(side="left", padx=4)

        cols = ("rank", "path", "symbols", "net_profit", "fees", "gross_final", "net_final", "profit_pct")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=17)
        widths = {"rank": 55, "path": 260, "symbols": 250, "net_profit": 110, "fees": 100, "gross_final": 110, "net_final": 110, "profit_pct": 100}
        for c in cols:
            self.tree.heading(c, text=c)
            anchor = "e" if c in {"net_profit", "fees", "gross_final", "net_final", "profit_pct"} else "w"
            self.tree.column(c, width=widths[c], anchor=anchor)
        self.tree.grid(row=3, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)
        self.tree.tag_configure("profit", foreground=self.theme["profit"])
        self.tree.tag_configure("loss", foreground=self.theme["loss"])
        self.tree.tag_configure("best", background="#dbeafe")

    def _build_tab_stats(self):
        f = self.tab_stats
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        ttk.Label(f, text="Historial de escaneos", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        cols = ("timestamp", "market", "network", "valid", "clean", "success", "best", "ms")
        self.hist_tree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        widths = {"timestamp": 165, "market": 90, "network": 90, "valid": 80, "clean": 80, "success": 95, "best": 95, "ms": 80}
        for c in cols:
            self.hist_tree.heading(c, text=c)
            self.hist_tree.column(c, width=widths[c], anchor="center")
        self.hist_tree.grid(row=1, column=0, sticky="nsew")

        controls = ttk.Frame(f)
        controls.grid(row=2, column=0, sticky="ew", pady=(8, 2))
        ttk.Label(controls, text="Ciclos (slider):").pack(side="left")
        self.cycle_slider = ttk.Scale(controls, from_=10, to=300, orient="horizontal", command=self._on_slider)
        self.cycle_slider.set(50)
        self.cycle_slider.pack(side="left", fill="x", expand=True, padx=8)

        ttk.Label(f, text="Detalle + gráfico ASCII", font=("Segoe UI", 11, "bold")).grid(row=3, column=0, sticky="w", pady=(6, 2))
        self.detail_text = tk.Text(f, height=12, wrap="word", font=("Consolas", 10))
        self.detail_text.grid(row=4, column=0, sticky="nsew")

    def _build_tab_exec(self):
        f = self.tab_exec
        f.columnconfigure(1, weight=1)
        f.rowconfigure(8, weight=1)
        f.rowconfigure(11, weight=1)

        ttk.Label(f, text="Binance Symbol").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.binance_symbol_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(f, text="Qty (USDT)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.binance_qty_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(f, text="Interval").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.binance_interval_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(f, text="Limit candles").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.binance_limit_var).grid(row=3, column=1, sticky="ew", pady=4)

        ttk.Checkbutton(f, text="Binance Testnet", variable=self.binance_testnet_var).grid(row=4, column=0, sticky="w", pady=4)
        ttk.Checkbutton(f, text="Marcar ejecución operativa", variable=self.binance_execute_var).grid(row=4, column=1, sticky="w", pady=4)

        flow = ttk.Frame(f)
        flow.grid(row=5, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(flow, text="Analizar IA Binance", command=self._run_binance_ai).pack(side="left", padx=4)
        ttk.Button(flow, text="Ejecutar flujo unificado", command=self._run_unified_flow).pack(side="left", padx=4)

        self.execution_text = tk.Text(f, height=10, wrap="word", font=("Consolas", 10))
        self.execution_text.grid(row=8, column=0, columnspan=2, sticky="nsew", pady=6)
        self.execution_text.tag_configure("INFO", foreground="#2563eb")
        self.execution_text.tag_configure("WARN", foreground="#d97706")
        self.execution_text.tag_configure("ERROR", foreground="#dc2626")

        ttk.Label(f, text="Traders ejecutados", font=("Segoe UI", 10, "bold")).grid(row=9, column=0, columnspan=2, sticky="w")
        cols = ("timestamp", "symbol", "signal", "best_path", "net_profit", "mode", "status")
        self.trade_tree = ttk.Treeview(f, columns=cols, show="headings", height=7)
        tw = {"timestamp": 165, "symbol": 90, "signal": 70, "best_path": 320, "net_profit": 100, "mode": 95, "status": 300}
        for c in cols:
            self.trade_tree.heading(c, text=c)
            self.trade_tree.column(c, width=tw[c], anchor="center")
        self.trade_tree.grid(row=11, column=0, columnspan=2, sticky="nsew", pady=4)

    def _bind_shortcuts(self):
        self.root.bind_all("<Control-s>", lambda _e: self._save_config())
        self.root.bind_all("<F5>", lambda _e: self._scan_once())
        self.root.bind_all("<Escape>", lambda _e: self._stop_autoscan())

    def _kpi_card(self, parent, title: str, var: tk.StringVar):
        card = ttk.Frame(parent, style="Card.TFrame", padding=8)
        ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=var, style="CardValue.TLabel").pack(anchor="w")
        return card

    def _set_busy(self, busy: bool):
        self._pulse_on = busy
        if busy:
            self.progress.start(10)
            self._update_status("Procesando operación...", "processing")
            self._pulse_dot()
        else:
            self.progress.stop()
            self._update_status("Listo", "info")

    def _pulse_dot(self):
        if not self._pulse_on:
            return
        current = self.dot.cget("fg")
        self.dot.configure(fg="#10b981" if current != "#10b981" else "#6b7280")
        self.root.after(400, self._pulse_dot)

    def _toast(self, message: str):
        if self.toast_window:
            self.toast_window.destroy()
        tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        x = self.root.winfo_rootx() + self.root.winfo_width() - 320
        y = self.root.winfo_rooty() + 60
        tw.geometry(f"300x40+{x}+{y}")
        tk.Label(tw, text=message, bg="#1f2937", fg="white").pack(fill="both", expand=True)
        self.toast_window = tw
        self.root.after(2200, lambda: tw.destroy() if tw.winfo_exists() else None)

    def _update_connection_indicator(self, state: str):
        if not hasattr(self, "dot"):
            return
        cmap = {"connected": "#198754", "disconnected": "#6c757d", "error": "#dc3545", "processing": "#ffc107", "info": "#0d6efd"}
        self.dot.configure(fg=cmap.get(state, "#6c757d"))

    def _update_status(self, message: str, level: str = "info"):
        self.status_var.set(message)
        self.status_type_var.set(level)
        if "error" in level.lower():
            self._update_connection_indicator("error")
        elif "success" in level.lower() or "ok" in message.lower():
            self._update_connection_indicator("connected")
        elif "processing" in level.lower():
            self._update_connection_indicator("processing")
        else:
            self._update_connection_indicator("info")

    def _show_help(self):
        messagebox.showinfo("Ayuda", "Atajos: Ctrl+S Guardar | F5 Escanear | Esc Detener AutoScan")

    def _copy_config(self):
        data = {
            "market": self.market_var.get(),
            "network": self.network_var.get(),
            "usdt": self.usdt_var.get(),
            "fee": self.fee_var.get(),
            "max_assets": self.max_assets_var.get(),
        }
        self.root.clipboard_clear()
        self.root.clipboard_append(json.dumps(data, ensure_ascii=False, indent=2))
        self._toast("Configuración copiada")

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
            "binance_symbol": self.binance_symbol_var.get().strip(),
            "binance_qty": self.binance_qty_var.get().strip(),
            "binance_interval": self.binance_interval_var.get().strip(),
            "binance_limit": self.binance_limit_var.get().strip(),
            "binance_testnet": self.binance_testnet_var.get(),
            "binance_execute": self.binance_execute_var.get(),
            "dark_mode": self.dark_mode.get(),
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.status_var.set("Configuración guardada")
        self._toast("Configuración guardada")

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
            self.binance_symbol_var.set(payload.get("binance_symbol", "BTCUSDT"))
            self.binance_qty_var.set(payload.get("binance_qty", "10"))
            self.binance_interval_var.set(payload.get("binance_interval", "1m"))
            self.binance_limit_var.set(payload.get("binance_limit", "300"))
            self.binance_testnet_var.set(bool(payload.get("binance_testnet", True)))
            self.binance_execute_var.set(bool(payload.get("binance_execute", False)))
            self.dark_mode.set(bool(payload.get("dark_mode", False)))
            self._apply_theme()
            self.status_var.set("Configuración cargada")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"No se pudo cargar configuración: {exc}")

    @staticmethod
    def _to_positive_float(raw: str, field: str) -> float:
        try:
            v = float(raw)
        except ValueError as exc:
            raise ValueError(f"{field} debe ser numérico. Ejemplo válido: 10.5") from exc
        if v <= 0:
            raise ValueError(f"{field} debe ser > 0")
        return v

    @staticmethod
    def _to_positive_int(raw: str, field: str) -> int:
        try:
            v = int(raw)
        except ValueError as exc:
            raise ValueError(f"{field} debe ser entero. Ejemplo válido: 120") from exc
        if v <= 0:
            raise ValueError(f"{field} debe ser > 0")
        return v

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
            self.root.after(0, lambda: self._set_busy(True))
            self._configure_scanner()
            start_usdt = self._to_positive_float(self.usdt_var.get(), "Capital")
            max_assets = self._to_positive_int(self.max_assets_var.get(), "Max assets")
            min_clean = float(self.min_clean_profit_var.get())
            output = self.scanner.scan(start_usdt=start_usdt, max_paths=40, max_assets=max_assets, min_clean_profit_usdt=min_clean)
            self.root.after(0, lambda: self._render_output(output))
            self.root.after(0, lambda: self.status_var.set(f"Scan completado: {len(output.opportunities)} rutas limpias"))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: self.status_var.set(f"Error en scan: {exc}"))
            self.root.after(0, lambda: self._log_exec(f"ERROR | Scan | {exc}", "ERROR"))
        finally:
            self.root.after(0, lambda: self._set_busy(False))

    def _render_output(self, output: ScanOutput):
        self.last_output = output
        self.all_scan_rows = list(output.opportunities)
        self._apply_filter()

        self.kpi_scan_ms.set(f"{output.stats.scan_ms} ms")
        self.kpi_success.set(f"{output.stats.success_rate_pct:.2f}%")
        self.kpi_best.set(f"{output.stats.best_profit_pct:.4f}%")
        self.kpi_avg.set(f"{output.stats.avg_profit_pct:.4f}%")
        self.kpi_vars["opportunities"].set(str(len(output.opportunities)))
        self._append_history(output)

        if output.opportunities:
            self._toast(f"Mejor oportunidad: {output.opportunities[0].profit_pct:.4f}%")

    def _apply_filter(self):
        q = self.search_var.get().strip().lower()
        for item in self.tree.get_children():
            self.tree.delete(item)

        rows = self.all_scan_rows
        if q:
            rows = [r for r in rows if q in " ".join(r.path).lower() or q in " ".join(r.symbols).lower()]

        for i, row in enumerate(rows, start=1):
            values = (
                i,
                " -> ".join(row.path),
                " / ".join(row.symbols),
                f"{row.net_profit_usdt:.6f}",
                f"{row.total_fees_usdt:.6f}",
                f"{row.gross_final_usdt:.6f}",
                f"{row.final_usdt:.6f}",
                f"{row.profit_pct:.4f}%",
            )
            tags = []
            tags.append("profit" if row.net_profit_usdt >= 0 else "loss")
            if i == 1:
                tags.append("best")
            self.tree.insert("", "end", values=values, tags=tuple(tags))

    def _append_history(self, output: ScanOutput):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
        self.scan_history = self.scan_history[-300:]

        for item in self.hist_tree.get_children():
            self.hist_tree.delete(item)
        for item in self.scan_history[-100:]:
            self.hist_tree.insert("", "end", values=item)

        self._render_ascii_chart()

    def _render_ascii_chart(self):
        if not self.scan_history:
            return
        bests = []
        for r in self.scan_history[-20:]:
            bests.append(float(str(r[6]).replace("%", "")))

        max_abs = max([abs(x) for x in bests] + [1.0])
        chart_lines = ["ASCII Profit Chart (últimos 20 scans):"]
        for v in bests:
            n = int((abs(v) / max_abs) * 20)
            bar = ("+" * n) if v >= 0 else ("-" * n)
            chart_lines.append(f"{v:>8.4f}% | {bar}")

        existing = self.detail_text.get("1.0", tk.END)
        if "ASCII Profit Chart" not in existing:
            self.detail_text.insert("end", "\n" + "\n".join(chart_lines) + "\n")

    def _on_slider(self, value):
        self.compound_cycles_var.set(str(int(float(value))))

    def _on_row_select(self, _event):
        if not self.last_output:
            return
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        if not values:
            return

        idx = int(values[0]) - 1
        rows = self.all_scan_rows
        if idx < 0 or idx >= len(rows):
            return

        row = rows[idx]
        plan = self.scanner.simulate_compound_plan(
            initial_capital_usdt=self._to_positive_float(self.usdt_var.get(), "Capital"),
            per_cycle_net_pct=row.profit_pct,
            cycles=self._to_positive_int(self.compound_cycles_var.get(), "Ciclos"),
            trigger_multiple=self._to_positive_float(self.compound_trigger_multiple_var.get(), "Trigger"),
            compound_stake_pct=self._to_positive_float(self.compound_stake_pct_var.get(), "Stake"),
            pre_trigger_stake_pct=1.0,
        )

        text = (
            f"Ruta: {' -> '.join(row.path)}\n"
            f"Símbolos: {' / '.join(row.symbols)}\n"
            f"Ganancia neta limpia: {row.net_profit_usdt:.6f} USDT ({row.profit_pct:.4f}%)\n"
            f"Comisiones: {row.total_fees_usdt:.6f} USDT\n\n"
            f"Compuesto -> Capital final: {plan.current_capital:.4f} USDT\n"
            f"Ciclos: {plan.cycles_simulated} | Trigger: {plan.trigger_reached} (ciclo {plan.trigger_cycle})\n"
            f"Modo stake: {plan.stake_mode}\n\n"
        )
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", text)
        self._render_ascii_chart()

    def _log_exec(self, line: str, level: str = "INFO"):
        tag = "INFO"
        if level.upper().startswith("WARN"):
            tag = "WARN"
        elif level.upper().startswith("ERR"):
            tag = "ERROR"
        self.execution_text.insert("end", f"{line}\n", tag)
        self.execution_text.see("end")

    def _record_trade(self, symbol: str, signal: str, best_path: str, net_profit: float, mode: str, status: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        row = (ts, symbol, signal, best_path, f"{net_profit:.6f}", mode, status)
        self.executed_trades.append(row)
        self.executed_trades = self.executed_trades[-300:]

        for item in self.trade_tree.get_children():
            self.trade_tree.delete(item)
        for item in self.executed_trades[-120:]:
            self.trade_tree.insert("", "end", values=item)

    def _run_binance_ai(self):
        threading.Thread(target=self._run_binance_ai_worker, daemon=True).start()

    def _run_binance_ai_worker(self):
        try:
            self.root.after(0, lambda: self._set_busy(True))
            symbol = self.binance_symbol_var.get().strip().upper()
            interval = self.binance_interval_var.get().strip()
            limit = self._to_positive_int(self.binance_limit_var.get(), "Limit")
            summary = self.ai_engine.run(symbol=symbol, interval=interval, limit=limit, exchange="binance")
            self.root.after(0, lambda: self._log_exec(f"INFO | AI Binance | {summary}", "INFO"))
            self.root.after(0, lambda: self.status_var.set(f"AI Binance OK: {summary['signal']}"))
            self.root.after(0, lambda: self._toast(f"Señal IA: {summary['signal']}"))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: self._log_exec(f"ERROR | AI Binance | {exc}", "ERROR"))
            self.root.after(0, lambda: self.status_var.set(f"Error AI Binance: {exc}"))
        finally:
            self.root.after(0, lambda: self._set_busy(False))

    def _run_unified_flow(self):
        if self.binance_execute_var.get():
            if not messagebox.askyesno("Confirmar", "¿Confirmas ejecución operativa (modo marcado)?"):
                return
        threading.Thread(target=self._run_unified_flow_worker, daemon=True).start()

    def _run_unified_flow_worker(self):
        try:
            self.root.after(0, lambda: self._set_busy(True))
            symbol = self.binance_symbol_var.get().strip().upper()
            interval = self.binance_interval_var.get().strip()
            limit = self._to_positive_int(self.binance_limit_var.get(), "Limit")
            qty = self._to_positive_float(self.binance_qty_var.get(), "Qty")
            execute = self.binance_execute_var.get()

            summary = self.ai_engine.run(symbol=symbol, interval=interval, limit=limit, exchange="binance")
            self.root.after(0, lambda: self._log_exec(f"INFO | 1) Señal IA: {summary}", "INFO"))

            self.scanner.configure(
                market_type=self.market_var.get().strip(),
                testnet=self.binance_testnet_var.get(),
                fee_rate=float(self.fee_var.get()),
            )
            scan = self.scanner.scan(
                start_usdt=qty,
                max_paths=5,
                max_assets=self._to_positive_int(self.max_assets_var.get(), "Max assets"),
                min_clean_profit_usdt=float(self.min_clean_profit_var.get()),
            )

            if scan.opportunities:
                best = scan.opportunities[0]
                path = " -> ".join(best.path)
                net = best.net_profit_usdt
                self.root.after(0, lambda: self._log_exec(f"INFO | 2) Mejor ruta: {path} | net={net:.6f} USDT", "INFO"))
                status = "OK"
            else:
                path = "N/A"
                net = 0.0
                self.root.after(0, lambda: self._log_exec("WARN | 2) Sin rutas limpias", "WARN"))
                status = "SIN_RUTAS"

            mode = "OPERATIVO" if execute else "SIM"
            self.root.after(0, lambda: self._record_trade(symbol, summary["signal"], path, net, mode, status))
            self.root.after(0, lambda: self._log_exec(f"INFO | 3) Registro trader guardado. mode={mode}", "INFO"))
            self.root.after(0, lambda: self.status_var.set(f"Flujo unificado OK ({summary['signal']})"))
            self.root.after(0, lambda: self._toast("Flujo unificado completado"))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: self._log_exec(f"ERROR | Flujo unificado | {exc}", "ERROR"))
            self.root.after(0, lambda: self.status_var.set(f"Error flujo unificado: {exc}"))
        finally:
            self.root.after(0, lambda: self._set_busy(False))

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
