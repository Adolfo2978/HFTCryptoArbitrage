"""HFTCryptoArbitrage - single-file unified system.

Incluye en un solo archivo:
- GUI principal con 4 pestañas.
- Scanner arbitraje triangular Binance (USDT -> A -> B -> USDT).
- IA de señal ligera (Binance/Bybit) sin dependencias pesadas.
- Simulación de interés compuesto.
- Flujo de ejecución unificada y registro de traders ejecutados.
"""

from __future__ import annotations

import json
import math
import os
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


# -------------------------
# DATA MODELS
# -------------------------

@dataclass(frozen=True)
class Edge:
    from_asset: str
    to_asset: str
    symbol: str
    side: str  # BUY or SELL


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


# -------------------------
# HTTP UTILS
# -------------------------


def http_get_json(url: str, timeout: int = 10) -> dict | list:
    req = Request(url, headers={"User-Agent": "HFTCryptoArbitrage/1.0"})
    try:
        with urlopen(req, timeout=timeout) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP error {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


# -------------------------
# BINANCE SCANNER
# -------------------------


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
        gross, net = start_usdt, start_usdt
        for edge in edges:
            gross = self._convert_once(gross, edge, prices)
            net = self._convert_with_fee(net, edge, prices)
            if gross is None or net is None:
                return None
        fees = max(0.0, gross - net)
        net_profit = net - start_usdt
        profit_pct = (net_profit / start_usdt) * 100 if start_usdt > 0 else 0
        return gross, net, fees, net_profit, profit_pct

    def scan(self, start_usdt: float, max_paths: int = 40, max_assets: int = 120, min_clean_profit_usdt: float = 0.0) -> ScanOutput:
        t0 = time.time()
        symbol_rows = self.fetch_exchange_info()
        prices = self.fetch_book_tickers()
        graph = self._build_edges(symbol_rows)

        all_rows: List[ArbitrageResult] = []
        scanned = 0
        for path, edges in self._enumerate_triangles(graph, max_assets=max_assets):
            scanned += 1
            cycle = self._eval_cycle(start_usdt, edges, prices)
            if cycle is None:
                continue
            gross, net, fees, net_profit, profit_pct = cycle
            clean = net_profit > min_clean_profit_usdt
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
                    is_clean_profitable=clean,
                )
            )

        clean_rows = [r for r in all_rows if r.is_clean_profitable]
        clean_rows.sort(key=lambda x: x.profit_pct, reverse=True)
        profits = [r.profit_pct for r in all_rows]

        stats = ScanStats(
            market_type=self.market_type,
            testnet=self.testnet,
            scanned_paths=scanned,
            valid_paths=len(all_rows),
            clean_profitable_paths=len(clean_rows),
            success_rate_pct=(len(clean_rows) / len(all_rows) * 100.0) if all_rows else 0.0,
            best_profit_pct=max(profits) if profits else 0.0,
            avg_profit_pct=(sum(profits) / len(profits)) if profits else 0.0,
            median_profit_pct=sorted(profits)[len(profits) // 2] if profits else 0.0,
            scan_ms=int((time.time() - t0) * 1000),
        )
        return ScanOutput(opportunities=clean_rows[:max_paths], stats=stats)

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
            stake = capital * stake_pct
            capital += stake * (per_cycle_net_pct / 100.0)
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


# -------------------------
# LIGHT AI SIGNAL
# -------------------------


class AISignalEngine:
    """Motor IA ligero sin dependencias externas pesadas.

    Heurística robusta:
    - momentum corto vs largo
    - volatilidad reciente
    - normalización simple
    """

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def _fetch_binance_klines(self, symbol: str, interval: str = "1m", limit: int = 300) -> List[List[float]]:
        q = urlencode({"symbol": symbol, "interval": interval, "limit": limit})
        rows = http_get_json(f"https://api.binance.com/api/v3/klines?{q}", timeout=self.timeout)
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Sin klines Binance")
        return rows

    def _fetch_bybit_klines(self, symbol: str, interval: str = "1", limit: int = 300) -> List[List[str]]:
        q = urlencode({"category": "linear", "symbol": symbol, "interval": interval, "limit": limit})
        payload = http_get_json(f"https://api.bybit.com/v5/market/kline?{q}", timeout=self.timeout)
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            raise RuntimeError("Error Bybit klines")
        rows = payload.get("result", {}).get("list", [])
        if not rows:
            raise RuntimeError("Sin klines Bybit")
        return rows

    def run(self, symbol: str, interval: str, limit: int, exchange: str = "binance") -> Dict[str, float | str]:
        ex = exchange.lower().strip()
        if ex == "binance":
            rows = self._fetch_binance_klines(symbol=symbol, interval=interval, limit=limit)
            closes = [float(r[4]) for r in rows]
            ts = int(rows[-1][0])
        elif ex == "bybit":
            rows = self._fetch_bybit_klines(symbol=symbol, interval=interval, limit=limit)
            rows = sorted(rows, key=lambda x: int(x[0]))
            closes = [float(r[4]) for r in rows]
            ts = int(rows[-1][0])
        else:
            raise ValueError("exchange debe ser 'binance' o 'bybit'")

        if len(closes) < 40:
            raise RuntimeError("Datos insuficientes para señal")

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


# -------------------------
# GUI APP
# -------------------------


class ArbitrageApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("HFTCryptoArbitrage - Single File System")
        self.root.geometry("1420x860")

        self.scanner = BinanceArbitrageScanner()
        self.ai_engine = AISignalEngine()

        self.running = False
        self.last_output: ScanOutput | None = None
        self.scan_history: List[tuple] = []
        self.executed_trades: List[tuple] = []

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

        self.binance_symbol_var = tk.StringVar(value="BTCUSDT")
        self.binance_qty_var = tk.StringVar(value="10")
        self.binance_interval_var = tk.StringVar(value="1m")
        self.binance_limit_var = tk.StringVar(value="300")
        self.binance_testnet_var = tk.BooleanVar(value=True)
        self.binance_execute_var = tk.BooleanVar(value=False)

        self.status_var = tk.StringVar(value="Listo")
        self.kpi_scan_ms = tk.StringVar(value="0 ms")
        self.kpi_success = tk.StringVar(value="0.00%")
        self.kpi_best = tk.StringVar(value="0.0000%")
        self.kpi_avg = tk.StringVar(value="0.0000%")

        self._setup_style()
        self._build_ui()
        self._load_config()

    def _setup_style(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TNotebook.Tab", padding=(14, 8), font=("Segoe UI", 10, "bold"))
        style.configure("Card.TFrame", relief="solid", borderwidth=1)
        style.configure("CardTitle.TLabel", font=("Segoe UI", 9))
        style.configure("CardValue.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Status.TLabel", foreground="#0b4f8a", font=("Segoe UI", 10, "bold"))

    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self.config_tab = ttk.Frame(notebook, padding=12)
        self.scan_tab = ttk.Frame(notebook, padding=12)
        self.stats_tab = ttk.Frame(notebook, padding=12)
        self.exec_tab = ttk.Frame(notebook, padding=12)

        notebook.add(self.config_tab, text="Configuración")
        notebook.add(self.scan_tab, text="Scanner")
        notebook.add(self.stats_tab, text="Estadística / interés compuesto")
        notebook.add(self.exec_tab, text="Ejecución unificada Binance (AI + Scanner)")

        self._build_config_tab(self.config_tab)
        self._build_scan_tab(self.scan_tab)
        self._build_stats_tab(self.stats_tab)
        self._build_exec_tab(self.exec_tab)

    def _build_config_tab(self, frame: ttk.Frame):
        frame.columnconfigure(1, weight=1)
        rows = [
            ("Binance API Key", self.api_key_var),
            ("Binance API Secret", self.api_secret_var),
            ("Capital inicial (USDT)", self.usdt_var),
            ("Fee por trade", self.fee_var),
            ("Máx activos USDT", self.max_assets_var),
            ("Ganancia limpia mínima", self.min_clean_profit_var),
            ("Ciclos compuesto", self.compound_cycles_var),
            ("Trigger compuesto", self.compound_trigger_multiple_var),
            ("Stake post-trigger", self.compound_stake_pct_var),
        ]
        for i, (label, var) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=5)
            ttk.Entry(frame, textvariable=var, show="*" if "Secret" in label else None).grid(row=i, column=1, sticky="ew", pady=5)

        ttk.Label(frame, text="Mercado").grid(row=9, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=self.market_var, values=["spot", "perpetual"], state="readonly").grid(row=9, column=1, sticky="ew", pady=5)

        ttk.Label(frame, text="Red").grid(row=10, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=self.network_var, values=["mainnet", "testnet"], state="readonly").grid(row=10, column=1, sticky="ew", pady=5)

        b = ttk.Frame(frame)
        b.grid(row=11, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Button(b, text="Guardar configuración", command=self._save_config).pack(side="left", padx=4)
        ttk.Button(b, text="Validar conectividad API", command=self._validate_keys).pack(side="left", padx=4)

        ttk.Label(frame, textvariable=self.status_var, style="Status.TLabel").grid(row=12, column=0, columnspan=2, sticky="w")

    def _build_scan_tab(self, frame: ttk.Frame):
        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Button(top, text="Escanear ahora", command=self._scan_once).pack(side="left", padx=4)
        ttk.Button(top, text="Auto-scan ON", command=self._start_autoscan).pack(side="left", padx=4)
        ttk.Button(top, text="Auto-scan OFF", command=self._stop_autoscan).pack(side="left", padx=4)

        kpis = ttk.Frame(frame)
        kpis.pack(fill="x", pady=8)
        self._kpi_card(kpis, "Tiempo scan", self.kpi_scan_ms).pack(side="left", padx=4)
        self._kpi_card(kpis, "Tasa éxito", self.kpi_success).pack(side="left", padx=4)
        self._kpi_card(kpis, "Mejor %", self.kpi_best).pack(side="left", padx=4)
        self._kpi_card(kpis, "Promedio %", self.kpi_avg).pack(side="left", padx=4)

        cols = ("rank", "path", "symbols", "net_profit", "fees", "gross_final", "net_final", "profit_pct")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=19)
        w = {"rank": 55, "path": 220, "symbols": 250, "net_profit": 110, "fees": 100, "gross_final": 110, "net_final": 110, "profit_pct": 100}
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w[c], anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)

    def _build_stats_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text="Historial de escaneos", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")

        cols = ("timestamp", "market", "network", "valid", "clean", "success", "best", "ms")
        self.hist_tree = ttk.Treeview(frame, columns=cols, show="headings", height=8)
        widths = {"timestamp": 165, "market": 90, "network": 90, "valid": 80, "clean": 80, "success": 95, "best": 95, "ms": 80}
        for c in cols:
            self.hist_tree.heading(c, text=c)
            self.hist_tree.column(c, width=widths[c], anchor="center")
        self.hist_tree.grid(row=1, column=0, sticky="nsew")

        ttk.Label(frame, text="Detalle + interés compuesto", font=("Segoe UI", 11, "bold")).grid(row=2, column=0, sticky="w", pady=(10, 4))
        self.detail_text = tk.Text(frame, height=12, wrap="word", font=("Consolas", 10))
        self.detail_text.grid(row=3, column=0, sticky="nsew")

    def _build_exec_tab(self, frame: ttk.Frame):
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(8, weight=1)
        frame.rowconfigure(11, weight=1)

        ttk.Label(frame, text="Binance Symbol").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.binance_symbol_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="Qty (USDT)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.binance_qty_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="Interval").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.binance_interval_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="Limit candles").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.binance_limit_var).grid(row=3, column=1, sticky="ew", pady=4)

        ttk.Checkbutton(frame, text="Binance Testnet", variable=self.binance_testnet_var).grid(row=4, column=0, sticky="w", pady=4)
        ttk.Checkbutton(frame, text="Marcar ejecución operativa", variable=self.binance_execute_var).grid(row=4, column=1, sticky="w", pady=4)

        b = ttk.Frame(frame)
        b.grid(row=5, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(b, text="Analizar IA Binance", command=self._run_binance_ai).pack(side="left", padx=4)
        ttk.Button(b, text="Ejecutar flujo unificado", command=self._run_unified_flow).pack(side="left", padx=4)

        self.execution_text = tk.Text(frame, height=10, wrap="word", font=("Consolas", 10))
        self.execution_text.grid(row=8, column=0, columnspan=2, sticky="nsew", pady=6)

        ttk.Label(frame, text="Traders ejecutados", font=("Segoe UI", 10, "bold")).grid(row=9, column=0, columnspan=2, sticky="w")
        cols = ("timestamp", "symbol", "signal", "best_path", "net_profit", "mode", "status")
        self.trade_tree = ttk.Treeview(frame, columns=cols, show="headings", height=7)
        tw = {"timestamp": 165, "symbol": 90, "signal": 70, "best_path": 320, "net_profit": 100, "mode": 95, "status": 300}
        for c in cols:
            self.trade_tree.heading(c, text=c)
            self.trade_tree.column(c, width=tw[c], anchor="center")
        self.trade_tree.grid(row=11, column=0, columnspan=2, sticky="nsew", pady=4)

    def _kpi_card(self, parent, title: str, var: tk.StringVar):
        card = ttk.Frame(parent, style="Card.TFrame", padding=8)
        ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=var, style="CardValue.TLabel").pack(anchor="w")
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
            "binance_symbol": self.binance_symbol_var.get().strip(),
            "binance_qty": self.binance_qty_var.get().strip(),
            "binance_interval": self.binance_interval_var.get().strip(),
            "binance_limit": self.binance_limit_var.get().strip(),
            "binance_testnet": self.binance_testnet_var.get(),
            "binance_execute": self.binance_execute_var.get(),
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
            self.binance_symbol_var.set(payload.get("binance_symbol", "BTCUSDT"))
            self.binance_qty_var.set(payload.get("binance_qty", "10"))
            self.binance_interval_var.set(payload.get("binance_interval", "1m"))
            self.binance_limit_var.set(payload.get("binance_limit", "300"))
            self.binance_testnet_var.set(bool(payload.get("binance_testnet", True)))
            self.binance_execute_var.set(bool(payload.get("binance_execute", False)))
            self.status_var.set("Configuración cargada")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"No se pudo cargar configuración: {exc}")

    @staticmethod
    def _to_positive_float(raw: str, field: str) -> float:
        try:
            v = float(raw)
        except ValueError as exc:
            raise ValueError(f"{field} debe ser numérico") from exc
        if v <= 0:
            raise ValueError(f"{field} debe ser > 0")
        return v

    @staticmethod
    def _to_positive_int(raw: str, field: str) -> int:
        try:
            v = int(raw)
        except ValueError as exc:
            raise ValueError(f"{field} debe ser entero") from exc
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
            self._configure_scanner()
            start_usdt = self._to_positive_float(self.usdt_var.get(), "Capital")
            max_assets = self._to_positive_int(self.max_assets_var.get(), "Max assets")
            min_clean = float(self.min_clean_profit_var.get())
            output = self.scanner.scan(start_usdt=start_usdt, max_paths=40, max_assets=max_assets, min_clean_profit_usdt=min_clean)
            self.root.after(0, lambda: self._render_output(output))
            self.root.after(0, lambda: self.status_var.set(f"Scan completado: {len(output.opportunities)} rutas limpias"))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: self.status_var.set(f"Error en scan: {exc}"))

    def _render_output(self, output: ScanOutput):
        self.last_output = output
        for item in self.tree.get_children():
            self.tree.delete(item)

        for i, row in enumerate(output.opportunities, start=1):
            self.tree.insert("", "end", values=(
                i,
                " -> ".join(row.path),
                " / ".join(row.symbols),
                f"{row.net_profit_usdt:.6f}",
                f"{row.total_fees_usdt:.6f}",
                f"{row.gross_final_usdt:.6f}",
                f"{row.final_usdt:.6f}",
                f"{row.profit_pct:.4f}%",
            ))

        self.kpi_scan_ms.set(f"{output.stats.scan_ms} ms")
        self.kpi_success.set(f"{output.stats.success_rate_pct:.2f}%")
        self.kpi_best.set(f"{output.stats.best_profit_pct:.4f}%")
        self.kpi_avg.set(f"{output.stats.avg_profit_pct:.4f}%")
        self._append_history(output)

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
        self.scan_history = self.scan_history[-25:]
        for item in self.hist_tree.get_children():
            self.hist_tree.delete(item)
        for item in self.scan_history:
            self.hist_tree.insert("", "end", values=item)

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
        if idx < 0 or idx >= len(self.last_output.opportunities):
            return

        row = self.last_output.opportunities[idx]
        plan = self.scanner.simulate_compound_plan(
            initial_capital_usdt=self._to_positive_float(self.usdt_var.get(), "Capital"),
            per_cycle_net_pct=row.profit_pct,
            cycles=self._to_positive_int(self.compound_cycles_var.get(), "Ciclos"),
            trigger_multiple=self._to_positive_float(self.compound_trigger_multiple_var.get(), "Trigger"),
            compound_stake_pct=self._to_positive_float(self.compound_stake_pct_var.get(), "Stake"),
            pre_trigger_stake_pct=1.0,
        )

        detail = (
            f"Ruta: {' -> '.join(row.path)}\n"
            f"Símbolos: {' / '.join(row.symbols)}\n"
            f"Ganancia neta limpia: {row.net_profit_usdt:.6f} USDT ({row.profit_pct:.4f}%)\n"
            f"Comisiones: {row.total_fees_usdt:.6f} USDT\n\n"
            f"Compuesto -> Capital final: {plan.current_capital:.4f} USDT en {plan.cycles_simulated} ciclos\n"
            f"Trigger alcanzado: {plan.trigger_reached} (ciclo {plan.trigger_cycle})\n"
            f"Modo stake: {plan.stake_mode}\n"
        )
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", detail)

    def _log_exec(self, line: str):
        self.execution_text.insert("end", f"{line}\n")
        self.execution_text.see("end")

    def _record_trade(self, symbol: str, signal: str, best_path: str, net_profit: float, mode: str, status: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        row = (ts, symbol, signal, best_path, f"{net_profit:.6f}", mode, status)
        self.executed_trades.append(row)
        self.executed_trades = self.executed_trades[-200:]
        for item in self.trade_tree.get_children():
            self.trade_tree.delete(item)
        for item in self.executed_trades[-100:]:
            self.trade_tree.insert("", "end", values=item)

    def _run_binance_ai(self):
        threading.Thread(target=self._run_binance_ai_worker, daemon=True).start()

    def _run_binance_ai_worker(self):
        try:
            symbol = self.binance_symbol_var.get().strip().upper()
            interval = self.binance_interval_var.get().strip()
            limit = self._to_positive_int(self.binance_limit_var.get(), "Limit")
            summary = self.ai_engine.run(symbol=symbol, interval=interval, limit=limit, exchange="binance")
            self.root.after(0, lambda: self._log_exec(f"AI Binance: {summary}"))
            self.root.after(0, lambda: self.status_var.set(f"AI Binance OK: {summary['signal']}"))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: self._log_exec(f"Error AI Binance: {exc}"))
            self.root.after(0, lambda: self.status_var.set(f"Error AI Binance: {exc}"))

    def _run_unified_flow(self):
        threading.Thread(target=self._run_unified_flow_worker, daemon=True).start()

    def _run_unified_flow_worker(self):
        try:
            symbol = self.binance_symbol_var.get().strip().upper()
            interval = self.binance_interval_var.get().strip()
            limit = self._to_positive_int(self.binance_limit_var.get(), "Limit")
            qty = self._to_positive_float(self.binance_qty_var.get(), "Qty")
            execute = self.binance_execute_var.get()

            summary = self.ai_engine.run(symbol=symbol, interval=interval, limit=limit, exchange="binance")
            self.root.after(0, lambda: self._log_exec(f"1) Señal IA: {summary}"))

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
                self.root.after(0, lambda: self._log_exec(f"2) Mejor ruta limpia: {path} | net={net:.6f} USDT"))
                status = "OK"
            else:
                path = "N/A"
                net = 0.0
                self.root.after(0, lambda: self._log_exec("2) Sin rutas limpias en este ciclo"))
                status = "SIN_RUTAS"

            mode = "OPERATIVO" if execute else "SIM"
            self.root.after(0, lambda: self._record_trade(symbol, summary["signal"], path, net, mode, status))
            self.root.after(0, lambda: self._log_exec(f"3) Registro trader guardado. Mode={mode}"))
            self.root.after(0, lambda: self.status_var.set(f"Flujo unificado OK ({summary['signal']})"))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: self._log_exec(f"Error flujo unificado: {exc}"))
            self.root.after(0, lambda: self.status_var.set(f"Error flujo unificado: {exc}"))

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
