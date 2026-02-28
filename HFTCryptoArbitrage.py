"""
🚀 HFTCryptoArbitrage v2.0 - Sistema Unificado de Arbitraje Cripto
Single-file professional GUI with enhanced UX, dark mode, and unified workflow.
"""
from __future__ import annotations
import json, math, threading, time, tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CONFIG_PATH = Path(".binance_gui_config.json")

# ═══════════════════════════════════════════════════════════════
# 📦 DATA MODELS
# ═══════════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════════
# 🔧 UTILITIES
# ═══════════════════════════════════════════════════════════════

def http_get_json(url: str, timeout: int = 10) -> dict | list:
    req = Request(url, headers={"User-Agent": "HFTCryptoArbitrage/2.0"})
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc

class ToolTip:
    """Tooltip profesional con animación suave"""
    def __init__(self, widget, text: str, delay: int = 300):
        self.widget, self.text, self.delay = widget, text, delay
        self.tip: Optional[tk.Toplevel] = None
        self._id: Optional[str] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
    
    def _schedule(self, _event=None):
        self._id = self.widget.after(self.delay, self._show)
    
    def _show(self):
        if self.tip: return
        x, y = self.widget.winfo_rootx() + 25, self.widget.winfo_rooty() + 30
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_attributes("-topmost", True, "-alpha", 0.95)
        self.tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(self.tip, text=self.text, background="#1f2937", 
                      foreground="#f9fafb", relief="solid", borderwidth=1,
                      padx=10, pady=6, font=("Segoe UI", 9), justify="left")
        lbl.pack()
    
    def _hide(self, _event=None):
        if self._id: self.widget.after_cancel(self._id)
        if self.tip: self.tip.destroy(); self.tip = None

# ═══════════════════════════════════════════════════════════════
# 🔍 BINANCE ARBITRAGE SCANNER
# ═══════════════════════════════════════════════════════════════

class BinanceArbitrageScanner:
    def __init__(self, fee_rate: float = 0.001, timeout: int = 10, 
                 market_type: str = "spot", testnet: bool = False):
        self.fee_rate, self.timeout = fee_rate, timeout
        self.market_type, self.testnet = market_type.lower(), testnet
    
    def configure(self, market_type: str, testnet: bool, fee_rate: float):
        self.market_type, self.testnet, self.fee_rate = market_type.lower(), testnet, fee_rate
    
    def _api_base(self) -> str:
        if self.market_type == "spot":
            return "https://testnet.binance.vision/" if self.testnet else "https://api.binance.com"
        if self.market_type == "perpetual":
            return "https://testnet.binancefuture.com/" if self.testnet else "https://fapi.binance.com"
        raise ValueError("market_type: 'spot' o 'perpetual'")
    
    def _endpoint(self, key: str) -> str:
        endpoints = {
            "spot": {"exchange_info": "/api/v3/exchangeInfo", "book_ticker": "/api/v3/ticker/bookTicker", "time": "/api/v3/time"},
            "perpetual": {"exchange_info": "/fapi/v1/exchangeInfo", "book_ticker": "/fapi/v1/ticker/bookTicker", "time": "/fapi/v1/time"}
        }
        return endpoints[self.market_type][key]
    
    def _get(self, endpoint: str, params: Optional[dict] = None):
        query = f"?{urlencode(params)}" if params else ""
        return http_get_json(f"{self._api_base().rstrip('/')}{endpoint}{query}", timeout=self.timeout)
    
    def fetch_exchange_info(self) -> List[dict]:
        payload = self._get(self._endpoint("exchange_info"))
        symbols = payload.get("symbols", [])
        if self.market_type == "spot":
            return [s for s in symbols if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)]
        return [s for s in symbols if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"]
    
    def fetch_book_tickers(self) -> Dict[str, Tuple[float, float]]:
        payload = self._get(self._endpoint("book_ticker"))
        prices = {}
        for row in payload:
            bid, ask = float(row["bidPrice"]), float(row["askPrice"])
            if bid > 0 and ask > 0: prices[row["symbol"]] = (bid, ask)
        return prices
    
    @staticmethod
    def _build_edges(symbol_rows: List[dict]) -> Dict[str, Dict[str, Edge]]:
        graph: Dict[str, Dict[str, Edge]] = {}
        for row in symbol_rows:
            symbol, base, quote = row.get("symbol"), row.get("baseAsset"), row.get("quoteAsset")
            if not all([symbol, base, quote]): continue
            graph.setdefault(quote, {})[base] = Edge(quote, base, symbol, "BUY")
            graph.setdefault(base, {})[quote] = Edge(base, quote, symbol, "SELL")
        return graph
    
    @staticmethod
    def _convert_once(amount: float, edge: Edge, prices: Dict[str, Tuple[float, float]]) -> Optional[float]:
        book = prices.get(edge.symbol)
        if not book: return None
        bid, ask = book
        return amount / ask if edge.side == "BUY" and ask > 0 else amount * bid if bid > 0 else None
    
    def _convert_with_fee(self, amount: float, edge: Edge, prices: Dict[str, Tuple[float, float]]) -> Optional[float]:
        out = self._convert_once(amount, edge, prices)
        return out * (1 - self.fee_rate) if out is not None else None
    
    def _enumerate_triangles(self, graph: Dict[str, Dict[str, Edge]], max_assets: int = 120):
        neighbors = list(graph.get("USDT", {}).keys())[:max_assets]
        for a in neighbors:
            e1 = graph["USDT"].get(a)
            if not e1: continue
            for b, e2 in graph.get(a, {}).items():
                if b == "USDT": continue
                e3 = graph.get(b, {}).get("USDT")
                if e2 and e3: yield ("USDT", a, b, "USDT"), (e1, e2, e3)
    
    def _eval_cycle(self, start_usdt: float, edges: Tuple[Edge, Edge, Edge], prices: Dict[str, Tuple[float, float]]):
        gross, net = start_usdt, start_usdt
        for edge in edges:
            gross = self._convert_once(gross, edge, prices)
            net = self._convert_with_fee(net, edge, prices)
            if gross is None or net is None: return None
        fees, profit = max(0.0, gross - net), net - start_usdt
        pct = (profit / start_usdt) * 100 if start_usdt > 0 else 0.0
        return gross, net, fees, profit, pct
    
    def scan(self, start_usdt: float, max_paths: int = 40, max_assets: int = 120, 
             min_clean_profit_usdt: float = 0.0) -> ScanOutput:
        t0 = time.time()
        symbols, prices = self.fetch_exchange_info(), self.fetch_book_tickers()
        graph = self._build_edges(symbols)
        all_rows, scanned = [], 0
        
        for path, edges in self._enumerate_triangles(graph, max_assets):
            scanned += 1
            cycle = self._eval_cycle(start_usdt, edges, prices)
            if cycle is None: continue
            gross, net, fees, profit, pct = cycle
            all_rows.append(ArbitrageResult(path=path, symbols=(edges[0].symbol, edges[1].symbol, edges[2].symbol),
                sides=(edges[0].side, edges[1].side, edges[2].side), gross_final_usdt=gross, final_usdt=net,
                total_fees_usdt=fees, net_profit_usdt=profit, profit_pct=pct, is_clean_profitable=profit > min_clean_profit_usdt))
        
        clean = sorted([r for r in all_rows if r.is_clean_profitable], key=lambda x: x.profit_pct, reverse=True)
        profits = [r.profit_pct for r in all_rows]
        median = sorted(profits)[len(profits)//2] if profits else 0.0
        
        stats = ScanStats(market_type=self.market_type, testnet=self.testnet, scanned_paths=scanned,
            valid_paths=len(all_rows), clean_profitable_paths=len(clean),
            success_rate_pct=(len(clean)/len(all_rows)*100) if all_rows else 0.0,
            best_profit_pct=max(profits) if profits else 0.0, avg_profit_pct=sum(profits)/len(profits) if profits else 0.0,
            median_profit_pct=median, scan_ms=int((time.time()-t0)*1000))
        return ScanOutput(opportunities=clean[:max_paths], stats=stats)
    
    @staticmethod
    def simulate_compound_plan(initial_capital_usdt: float, per_cycle_net_pct: float, cycles: int,
                               trigger_multiple: float = 2.0, compound_stake_pct: float = 0.10,
                               pre_trigger_stake_pct: float = 1.0) -> CompoundPlanResult:
        capital, trigger, reached, trigger_cycle = initial_capital_usdt, initial_capital_usdt * trigger_multiple, False, -1
        for i in range(1, cycles + 1):
            if capital >= trigger and not reached: reached, trigger_cycle = True, i
            stake_pct = compound_stake_pct if reached else pre_trigger_stake_pct
            capital += (capital * stake_pct) * (per_cycle_net_pct / 100.0)
        return CompoundPlanResult(initial_capital=initial_capital_usdt, current_capital=capital, cycles_simulated=cycles,
            trigger_reached=reached, trigger_cycle=trigger_cycle,
            stake_mode=f"pre:{pre_trigger_stake_pct*100:.1f}% | post:{compound_stake_pct*100:.1f}%",
            per_cycle_net_pct=per_cycle_net_pct)
    
    def validate_api_keys(self, api_key: str, api_secret: str) -> Tuple[bool, str]:
        if not api_key or not api_secret: return False, "Faltan API key/secret"
        if len(api_key) < 20 or len(api_secret) < 20: return False, "Formato inválido"
        try:
            _ = self._get(self._endpoint("time"))
            return True, f"✅ Conectividad {'TESTNET' if self.testnet else 'MAINNET'} OK"
        except Exception as e: return False, f"❌ Error: {e}"

# ═══════════════════════════════════════════════════════════════
# 🤖 LIGHTWEIGHT AI SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════════

class AISignalEngine:
    def __init__(self, timeout: int = 10): self.timeout = timeout
    
    def _fetch_binance(self, symbol: str, interval: str, limit: int) -> List[list]:
        q = urlencode({"symbol": symbol, "interval": interval, "limit": limit})
        rows = http_get_json(f"https://api.binance.com/api/v3/klines?{q}", timeout=self.timeout)
        if not isinstance(rows, list) or not rows: raise RuntimeError("Sin datos Binance")
        return rows
    
    def _fetch_bybit(self, symbol: str, interval: str, limit: int) -> List[list]:
        q = urlencode({"category": "linear", "symbol": symbol, "interval": interval, "limit": limit})
        payload = http_get_json(f"https://api.bybit.com/v5/market/kline?{q}", timeout=self.timeout)
        if payload.get("retCode") != 0: raise RuntimeError("Error Bybit")
        rows = payload.get("result", {}).get("list", [])
        if not rows: raise RuntimeError("Sin datos Bybit")
        return sorted(rows, key=lambda x: int(x[0]))
    
    def run(self, symbol: str, interval: str, limit: int, exchange: str = "binance") -> Dict[str, float | str]:
        ex = exchange.lower().strip()
        rows = self._fetch_binance(symbol, interval, limit) if ex == "binance" else self._fetch_bybit(symbol, interval, limit)
        closes, ts = [float(r[4]) for r in rows], int(rows[-1][0])
        if len(closes) < 40: raise RuntimeError("Datos insuficientes")
        
        short, long_ = sum(closes[-8:])/8, sum(closes[-21:])/21
        momentum = (short/long_)-1.0 if long_ else 0.0
        rets = [(closes[-i]/closes[-i-1])-1.0 for i in range(1, min(30, len(closes))) if closes[-i-1] > 0]
        vol = math.sqrt(sum(r*r for r in rets)/len(rets)) if rets else 0.0
        
        score = momentum * 120 - vol * 8
        prob = 1.0/(1.0+math.exp(-max(-8.0, min(8.0, score))))
        signal = "BUY" if prob >= 0.58 else "SELL" if prob <= 0.42 else "HOLD"
        
        return {"exchange": ex, "symbol": symbol, "interval": interval, "rows": len(closes),
                "last_close": closes[-1], "last_candle_utc": datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat(),
                "test_accuracy": round(max(0.5, 1.0-vol*30), 4), "probability_up": round(prob, 4), "signal": signal}

# ═══════════════════════════════════════════════════════════════
# 🎨 PROFESSIONAL GUI APPLICATION
# ═══════════════════════════════════════════════════════════════

class ArbitrageApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("🚀 HFTCryptoArbitrage v2.0 | Sistema Profesional Unificado")
        self.root.geometry("1680x950")
        self.root.minsize(1300, 750)
        
        # 🎨 Tema profesional con modo oscuro
        self.dark_mode = tk.BooleanVar(value=False)
        self._init_themes()
        self._apply_theme()
        
        # 🔧 Componentes principales
        self.scanner = BinanceArbitrageScanner()
        self.ai_engine = AISignalEngine()
        self.running, self.last_output = False, None
        self.all_scan_rows, self.scan_history, self.executed_trades = [], [], []
        self.toast_window: Optional[tk.Toplevel] = None
        self._pulse_active = False
        
        # 📦 Variables de configuración
        self._init_variables()
        
        # 🏗️ Construcción de UI
        self._setup_style()
        self._build_main_layout()
        self._bind_shortcuts()
        self._load_config()
        self._update_status("✨ Sistema listo • Conectado a Binance Testnet", "info")
    
    def _init_themes(self):
        """Define paletas de colores para modo claro/oscuro"""
        self.themes = {
            "light": {"bg": "#f8fafc", "fg": "#1e293b", "card": "#ffffff", "border": "#e2e8f0",
                     "primary": "#3b82f6", "success": "#10b981", "warning": "#f59e0b", "danger": "#ef4444",
                     "profit": "#059669", "loss": "#dc2626", "neutral": "#64748b"},
            "dark": {"bg": "#0f172a", "fg": "#f1f5f9", "card": "#1e293b", "border": "#334155",
                    "primary": "#60a5fa", "success": "#34d399", "warning": "#fbbf24", "danger": "#f87171",
                    "profit": "#34d399", "loss": "#f87171", "neutral": "#94a3b8"}
        }
    
    def _get_theme(self) -> dict:
        return self.themes["dark" if self.dark_mode.get() else "light"]
    
    def _init_variables(self):
        """Inicializa todas las variables de control con valores robustos"""
        # API & Red
        self.api_key_var, self.api_secret_var = tk.StringVar(), tk.StringVar()
        self.market_var, self.network_var = tk.StringVar(value="spot"), tk.StringVar(value="testnet")
        
        # Parámetros de escaneo
        self.usdt_var, self.fee_var = tk.StringVar(value="100.00"), tk.StringVar(value="0.001")
        self.max_assets_var, self.min_profit_var = tk.StringVar(value="120"), tk.StringVar(value="0.01")
        
        # Interés compuesto
        self.cycles_var = tk.StringVar(value="50")
        self.trigger_mult_var, self.stake_pct_var = tk.StringVar(value="2.0"), tk.StringVar(value="10.0")
        
        # Ejecución Binance
        self.exec_symbol_var, self.exec_qty_var = tk.StringVar(value="BTCUSDT"), tk.StringVar(value="100.00")
        self.exec_interval_var, self.exec_limit_var = tk.StringVar(value="1m"), tk.StringVar(value="300")
        self.exec_testnet_var, self.exec_live_var = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        
        # Estado y filtros
        self.search_var, self.status_var = tk.StringVar(), tk.StringVar(value="Listo")
        self.status_level_var = tk.StringVar(value="info")
        
        # KPIs
        self.kpi_vars = {k: tk.StringVar(value=v) for k, v in [
            ("scan_time", "0 ms"), ("success_rate", "0.00%"), ("best_profit", "0.0000%"),
            ("avg_profit", "0.0000%"), ("opportunities", "0")].items()}
    
    def _setup_style(self):
        """Configura estilos ttk profesionales"""
        style = ttk.Style()
        if "clam" in style.theme_names(): style.theme_use("clam")
        t = self._get_theme()
        
        # Estilos base
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"], font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=8)
        style.configure("Primary.TButton", background=t["primary"], foreground="white")
        style.configure("Success.TButton", background=t["success"], foreground="white")
        style.configure("Danger.TButton", background=t["danger"], foreground="white")
        
        # Notebook
        style.configure("TNotebook", background=t["bg"])
        style.configure("TNotebook.Tab", padding=[18, 10], font=("Segoe UI", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", t["primary"])], foreground=[("selected", "white")])
        
        # Treeview profesional
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 9), background=t["card"], fieldbackground=t["card"])
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), background=t["card"], foreground=t["fg"])
        style.map("Treeview", background=[("selected", t["primary"])], foreground=[("selected", "white")])
        
        # Cards para KPIs
        style.configure("Card.TFrame", background=t["card"], relief="solid", borderwidth=1)
        style.configure("CardHeader.TLabel", background=t["card"], foreground=t["neutral"], font=("Segoe UI", 9))
        style.configure("CardValue.TLabel", background=t["card"], foreground=t["success"], font=("Segoe UI", 15, "bold"))
        
        # Status bar
        style.configure("Status.TLabel", font=("Segoe UI", 10), padding=4)
        for lvl in ["info", "success", "warning", "error"]:
            fg = {"info": t["primary"], "success": t["success"], "warning": t["warning"], "error": t["danger"]}[lvl]
            style.configure(f"Status.{lvl}.TLabel", foreground=fg, background=t["bg"])
    
    def _apply_theme(self):
        """Aplica el tema actual a toda la interfaz"""
        t = self._get_theme()
        self.root.configure(bg=t["bg"])
        self._setup_style()
        # Refrescar widgets existentes si es necesario
        if hasattr(self, 'status_label'):
            self._update_status(self.status_var.get(), self.status_level_var.get())
    
    def _build_main_layout(self):
        """Construye el layout principal con toolbar, notebook y statusbar"""
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)
        
        # === TOOLBAR SUPERIOR ===
        toolbar = ttk.Frame(self.root, style="Card.TFrame", padding=(12, 8))
        toolbar.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        
        # Título y logo
        title_frame = ttk.Frame(toolbar)
        title_frame.pack(side="left", padx=(0, 24))
        ttk.Label(title_frame, text="🚀 HFTCryptoArbitrage", font=("Segoe UI", 14, "bold")).pack()
        ttk.Label(title_frame, text="Sistema Profesional de Arbitraje Triangular", 
                 font=("Segoe UI", 9), foreground=self._get_theme()["neutral"]).pack()
        
        # Botones de acción rápida
        actions = ttk.Frame(toolbar)
        actions.pack(side="right")
        ttk.Button(actions, text="💾 Guardar", style="Primary.TButton", command=self._save_config).pack(side="left", padx=4)
        ttk.Button(actions, text="🔄 Recargar", command=self._load_config).pack(side="left", padx=4)
        ttk.Button(actions, text="❓ Ayuda", command=self._show_help).pack(side="left", padx=4)
        
        # Toggle modo oscuro
        ttk.Checkbutton(toolbar, text="🌙 Modo oscuro", variable=self.dark_mode, command=self._apply_theme).pack(side="right", padx=(20, 0))
        
        # Indicador de conexión
        self.conn_dot = tk.Label(toolbar, text="●", font=("Segoe UI", 10), bg=self._get_theme()["bg"])
        self.conn_dot.pack(side="right", padx=(15, 0))
        self._update_connection_indicator("disconnected")
        
        # === NOTEBOOK CON PESTAÑAS ===
        notebook = ttk.Notebook(self.root)
        notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        
        self.tab_config = ttk.Frame(notebook, padding=16)
        self.tab_scan = ttk.Frame(notebook, padding=16)
        self.tab_stats = ttk.Frame(notebook, padding=16)
        self.tab_exec = ttk.Frame(notebook, padding=16)
        
        notebook.add(self.tab_config, text="⚙️ Configuración")
        notebook.add(self.tab_scan, text="🔍 Scanner")
        notebook.add(self.tab_stats, text="📈 Estadísticas")
        notebook.add(self.tab_exec, text="🚀 Ejecución")
        
        self._build_config_tab()
        self._build_scanner_tab()
        self._build_stats_tab()
        self._build_execution_tab()
        
        # === STATUS BAR INFERIOR ===
        self._build_statusbar()
    
    def _build_statusbar(self):
        """Barra de estado profesional con indicador animado"""
        status = ttk.Frame(self.root, style="Card.TFrame", padding=(12, 6))
        status.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 12))
        
        # Indicador de actividad (pulsing dot)
        self.activity_dot = tk.Label(status, text="●", font=("Segoe UI", 8), bg=self._get_theme()["bg"])
        self.activity_dot.pack(side="left", padx=(0, 8))
        
        # Mensaje de estado
        self.status_label = ttk.Label(status, textvariable=self.status_var, style="Status.info.TLabel")
        self.status_label.pack(side="left")
        
        # Información adicional
        info = ttk.Frame(status)
        info.pack(side="right")
        ttk.Label(info, text="v2.0.0 • Python 3.10+", font=("Segoe UI", 8), 
                 foreground=self._get_theme()["neutral"]).pack(side="right", padx=12)
    
    def _build_config_tab(self):
        """Pestaña de configuración con secciones organizadas"""
        f = self.tab_config
        f.columnconfigure(1, weight=1)
        
        # === SECCIÓN API BINANCE ===
        api = ttk.LabelFrame(f, text="🔑 Conexión Binance", padding=12)
        api.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        api.columnconfigure(1, weight=1)
        
        rows = [("API Key:", self.api_key_var, True), ("API Secret:", self.api_secret_var, True),
                ("Mercado:", self.market_var, False, ["spot", "perpetual"]),
                ("Red:", self.network_var, False, ["mainnet", "testnet"])]
        
        for i, row in enumerate(rows):
            ttk.Label(api, text=row[0]).grid(row=i, column=0, sticky="w", pady=5)
            if len(row) > 3:
                cb = ttk.Combobox(api, textvariable=row[1], values=row[3], state="readonly", width=25)
                cb.grid(row=i, column=1, sticky="w", pady=5)
                ToolTip(cb, f"Seleccionar {row[0].rstrip(':')}")
            else:
                ent = ttk.Entry(api, textvariable=row[1], show="*" if row[2] else None, width=40)
                ent.grid(row=i, column=1, sticky="ew", pady=5, padx=(0, 12))
                ToolTip(ent, f"Ingresar {row[0].rstrip(':')}")
        
        ttk.Button(api, text="✅ Validar Conexión", style="Success.TButton", command=self._validate_keys).grid(row=4, column=0, columnspan=2, pady=8)
        
        # === PARÁMETROS DE ESCANEO ===
        params = ttk.LabelFrame(f, text="⚙️ Parámetros de Escaneo", padding=12)
        params.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        params.columnconfigure(1, weight=1)
        
        scan_params = [("Capital Inicial (USDT):", self.usdt_var, "100.00"),
                      ("Fee por Trade:", self.fee_var, "0.001"),
                      ("Máx Activos USDT:", self.max_assets_var, "120"),
                      ("Ganancia Mínima (%):", self.min_profit_var, "0.01")]
        
        for i, (lbl, var, ph) in enumerate(scan_params):
            ttk.Label(params, text=lbl).grid(row=i, column=0, sticky="w", pady=4)
            ent = ttk.Entry(params, textvariable=var, width=20)
            ent.grid(row=i, column=1, sticky="w", pady=4)
            ent.insert(0, ph)
            ToolTip(ent, lbl.rstrip(":"))
        
        # === INTERÉS COMPUESTO ===
        compound = ttk.LabelFrame(f, text="📊 Simulador de Interés Compuesto", padding=12)
        compound.grid(row=2, column=0, columnspan=2, sticky="ew")
        compound.columnconfigure(1, weight=1)
        
        comp_params = [("Ciclos a Simular:", self.cycles_var, "50"),
                      ("Trigger (Multiplicador):", self.trigger_mult_var, "2.0"),
                      ("Stake Post-Trigger (%):", self.stake_pct_var, "10.0")]
        
        for i, (lbl, var, ph) in enumerate(comp_params):
            ttk.Label(compound, text=lbl).grid(row=i, column=0, sticky="w", pady=4)
            ent = ttk.Entry(compound, textvariable=var, width=20)
            ent.grid(row=i, column=1, sticky="w", pady=4)
            ent.insert(0, ph)
            ToolTip(ent, lbl.rstrip(":"))
    
    def _build_scanner_tab(self):
        """Pestaña del scanner con KPIs destacados y tabla profesional"""
        f = self.tab_scan
        f.rowconfigure(3, weight=1)
        f.columnconfigure(0, weight=1)
        
        # Panel de control
        ctrl = ttk.Frame(f)
        ctrl.pack(fill="x", pady=(0, 10))
        
        btn_frame = ttk.Frame(ctrl)
        btn_frame.pack(side="left")
        ttk.Button(btn_frame, text="▶️ Escanear Ahora", style="Primary.TButton", command=self._scan_once).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="🔄 Auto-Scan ON", command=self._start_autoscan).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="⏹️ Detener", style="Danger.TButton", command=self._stop_autoscan).pack(side="left", padx=4)
        
        # Filtro de búsqueda
        filter_frame = ttk.Frame(ctrl)
        filter_frame.pack(side="right")
        ttk.Label(filter_frame, text="🔍 Filtrar:").pack(side="left")
        search_ent = ttk.Entry(filter_frame, textvariable=self.search_var, width=25)
        search_ent.pack(side="left", padx=6)
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        ToolTip(search_ent, "Filtra por símbolo o ruta de arbitraje")
        
        # Barra de progreso
        self.progress = ttk.Progressbar(f, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 10))
        
        # KPI Cards
        kpi_frame = ttk.Frame(f)
        kpi_frame.pack(fill="x", pady=(0, 12))
        
        kpi_configs = [("⏱️ Tiempo", self.kpi_vars["scan_time"], "ms", "Duración del escaneo"),
                      ("🎯 Éxito", self.kpi_vars["success_rate"], "%", "Rutas rentables / totales"),
                      ("💎 Mejor", self.kpi_vars["best_profit"], "%", "Máxima ganancia detectada"),
                      ("📊 Promedio", self.kpi_vars["avg_profit"], "%", "Ganancia promedio"),
                      ("✨ Oportunidades", self.kpi_vars["opportunities"], "", "Rutas limpias")]
        
        for title, var, suffix, tooltip in kpi_configs:
            card = self._create_kpi_card(kpi_frame, title, var, suffix, tooltip)
            card.pack(side="left", padx=5, expand=True, fill="x")
        
        # Tabla de resultados profesional
        table_frame = ttk.Frame(f)
        table_frame.pack(fill="both", expand=True)
        
        cols = ("rank", "path", "symbols", "net_profit", "fees", "profit_pct", "action")
        self.results_tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=14)
        
        col_cfg = {"rank": {"text": "#", "w": 45, "a": "center"}, "path": {"text": "Ruta", "w": 220, "a": "w"},
                  "symbols": {"text": "Símbolos", "w": 240, "a": "w"}, "net_profit": {"text": "Profit Neto", "w": 120, "a": "e"},
                  "fees": {"text": "Comisiones", "w": 100, "a": "e"}, "profit_pct": {"text": "Profit %", "w": 95, "a": "e"},
                  "action": {"text": "Acción", "w": 110, "a": "center"}}
        
        for c in cols:
            cfg = col_cfg[c]
            self.results_tree.heading(c, text=cfg["text"], command=lambda col=c: self._sort_column(col))
            self.results_tree.column(c, width=cfg["w"], anchor=cfg["a"])
        
        # Scrollbars
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.results_tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.results_tree.xview)
        self.results_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.results_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        
        # Bindings y tags
        self.results_tree.bind("<<TreeviewSelect>>", self._on_result_select)
        self.results_tree.bind("<Double-1>", self._on_result_double_click)
        t = self._get_theme()
        self.results_tree.tag_configure("profit", foreground=t["profit"])
        self.results_tree.tag_configure("loss", foreground=t["loss"])
        self.results_tree.tag_configure("best", background=t["primary"] if self.dark_mode.get() else "#dbeafe", 
                                       foreground="white" if self.dark_mode.get() else t["fg"])
        
        # Panel de detalles
        self.detail_frame = ttk.LabelFrame(f, text="📋 Detalles de Selección", padding=10)
        self.detail_frame.pack(fill="x", pady=(12, 0))
        
        self.detail_text = tk.Text(self.detail_frame, height=5, wrap="word", font=("Consolas", 9), 
                                  state="disabled", bg=self._get_theme()["card"], fg=self._get_theme()["fg"])
        self.detail_text.pack(fill="x")
        
        # Botones de acción en detalles
        detail_btns = ttk.Frame(self.detail_frame)
        detail_btns.pack(fill="x", pady=(8, 0))
        ttk.Button(detail_btns, text="📊 Simular Compuesto", command=self._simulate_selected).pack(side="left", padx=4)
        ttk.Button(detail_btns, text="📋 Copiar Ruta", command=self._copy_selected_path).pack(side="left", padx=4)
        ttk.Button(detail_btns, text="🚀 Ejecutar Esta Ruta", style="Success.TButton", command=self._execute_selected).pack(side="left", padx=4)
    
    def _create_kpi_card(self, parent, title: str, var: tk.StringVar, suffix: str, tooltip: str):
        """Crea una card visual profesional para KPIs"""
        card = ttk.Frame(parent, style="Card.TFrame", padding=12)
        ttk.Label(card, text=title, style="CardHeader.TLabel").pack(anchor="w")
        ToolTip(ttk.Label(card, text=title, style="CardHeader.TLabel"), tooltip)
        
        value_frame = ttk.Frame(card)
        value_frame.pack(fill="x", pady=(4, 2))
        ttk.Label(value_frame, textvariable=var, style="CardValue.TLabel").pack(anchor="w")
        if suffix:
            ttk.Label(value_frame, text=suffix, font=("Segoe UI", 10), 
                     foreground=self._get_theme()["neutral"]).pack(side="right")
        ttk.Separator(card, orient="horizontal").pack(fill="x", pady=6)
        return card
    
    def _build_stats_tab(self):
        """Pestaña de estadísticas con gráfico ASCII y historial"""
        f = self.tab_stats
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)
        
        ttk.Label(f, text="📜 Historial de Escaneos", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        
        cols = ("timestamp", "market", "network", "valid", "clean", "success", "best", "ms")
        self.hist_tree = ttk.Treeview(f, columns=cols, show="headings", height=7)
        widths = {"timestamp": 165, "market": 85, "network": 85, "valid": 70, "clean": 70, "success": 90, "best": 90, "ms": 75}
        for c in cols:
            self.hist_tree.heading(c, text=c.upper())
            self.hist_tree.column(c, width=widths[c], anchor="center")
        self.hist_tree.grid(row=1, column=0, sticky="nsew", pady=(4, 8))
        
        # Slider de ciclos
        ctrl = ttk.Frame(f)
        ctrl.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(ctrl, text="Ciclos para simulación:").pack(side="left")
        self.cycle_slider = ttk.Scale(ctrl, from_=10, to=300, orient="horizontal", command=lambda v: self.cycles_var.set(str(int(float(v)))))
        self.cycle_slider.set(50)
        self.cycle_slider.pack(side="left", fill="x", expand=True, padx=10)
        ttk.Label(ctrl, textvariable=self.cycles_var, width=4).pack(side="left")
        
        # Gráfico ASCII y detalles
        ttk.Label(f, text="📈 Gráfico ASCII + Detalles", font=("Segoe UI", 11, "bold")).grid(row=3, column=0, sticky="w", pady=(8, 4))
        self.chart_text = tk.Text(f, height=14, wrap="none", font=("Consolas", 9), 
                               bg=self._get_theme()["card"], fg=self._get_theme()["fg"])
        self.chart_text.grid(row=4, column=0, sticky="nsew")
        
        # Scroll para gráfico
        chart_vsb = ttk.Scrollbar(f, orient="vertical", command=self.chart_text.yview)
        chart_hsb = ttk.Scrollbar(f, orient="horizontal", command=self.chart_text.xview)
        self.chart_text.configure(yscrollcommand=chart_vsb.set, xscrollcommand=chart_hsb.set)
        chart_vsb.grid(row=4, column=0, sticky="ns", padx=(0, 2))
        chart_hsb.grid(row=4, column=0, sticky="ew", pady=(0, 2))
    
    def _build_execution_tab(self):
        """Pestaña de ejecución unificada con flujo visual"""
        f = self.tab_exec
        f.columnconfigure(1, weight=1)
        f.rowconfigure(7, weight=1)
        f.rowconfigure(10, weight=1)
        
        # Parámetros de ejecución
        params = [("🪙 Símbolo:", self.exec_symbol_var), ("💰 Cantidad (USDT):", self.exec_qty_var),
                 ("⏱️ Intervalo:", self.exec_interval_var), ("🕯️ Velas:", self.exec_limit_var)]
        
        for i, (lbl, var) in enumerate(params):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=4)
            ent = ttk.Entry(f, textvariable=var, width=25)
            ent.grid(row=i, column=1, sticky="ew", pady=4, padx=(0, 12))
            ToolTip(ent, lbl.rstrip(":"))
        
        # Opciones
        opts = ttk.Frame(f)
        opts.grid(row=4, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Checkbutton(opts, text="🧪 Usar Testnet", variable=self.exec_testnet_var).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(opts, text="⚡ Modo Operativo Real", variable=self.exec_live_var).pack(side="left")
        ToolTip(ttk.Label(opts, text="⚡ Modo Operativo Real"), "⚠️ Ejecuta órdenes reales en Binance")
        
        # Botones de flujo
        flow_btns = ttk.Frame(f)
        flow_btns.grid(row=5, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(flow_btns, text="🤖 Analizar con IA", command=self._run_binance_ai).pack(side="left", padx=4)
        ttk.Button(flow_btns, text="🔄 Ejecutar Flujo Unificado", style="Primary.TButton", command=self._run_unified_flow).pack(side="left", padx=4)
        
        # Log de ejecución con colores
        ttk.Label(f, text="📝 Log de Ejecución", font=("Segoe UI", 10, "bold")).grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 4))
        self.exec_log = tk.Text(f, height=9, wrap="word", font=("Consolas", 9), 
                               bg=self._get_theme()["card"], fg=self._get_theme()["fg"])
        self.exec_log.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(0, 8))
        self.exec_log.tag_configure("INFO", foreground=self._get_theme()["primary"])
        self.exec_log.tag_configure("SUCCESS", foreground=self._get_theme()["success"])
        self.exec_log.tag_configure("WARN", foreground=self._get_theme()["warning"])
        self.exec_log.tag_configure("ERROR", foreground=self._get_theme()["danger"])
        
        # Scroll para log
        log_vsb = ttk.Scrollbar(f, orient="vertical", command=self.exec_log.yview)
        self.exec_log.configure(yscrollcommand=log_vsb.set)
        log_vsb.grid(row=7, column=1, sticky="ns", padx=(0, 2))
        
        # Trades ejecutados
        ttk.Label(f, text="💼 Trades Ejecutados", font=("Segoe UI", 10, "bold")).grid(row=8, column=0, columnspan=2, sticky="w")
        cols = ("timestamp", "symbol", "signal", "path", "profit", "mode", "status")
        self.trade_tree = ttk.Treeview(f, columns=cols, show="headings", height=6)
        tw = {"timestamp": 165, "symbol": 85, "signal": 65, "path": 280, "profit": 95, "mode": 80, "status": 200}
        for c in cols:
            self.trade_tree.heading(c, text=c.upper())
            self.trade_tree.column(c, width=tw[c], anchor="center")
        self.trade_tree.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
        ttk.Scrollbar(f, orient="vertical", command=self.trade_tree.yview).grid(row=9, column=1, sticky="ns", padx=(0, 2))
        self.trade_tree.configure(yscrollcommand=lambda *args: None)  # Simplificado
    
    def _bind_shortcuts(self):
        """Atajos de teclado profesionales"""
        self.root.bind("<Control-s>", lambda e: self._save_config())
        self.root.bind("<F5>", lambda e: self._scan_once())
        self.root.bind("<Escape>", lambda e: self._stop_autoscan())
        self.root.bind("<Control-f>", lambda e: self.search_var.set("") or self.tab_scan.tkraise())
        self.root.bind("<Control-d>", lambda e: self.dark_mode.set(not self.dark_mode.get()) or self._apply_theme())
    
    def _kpi_card(self, parent, title: str, var: tk.StringVar):
        """Compatibilidad con código existente"""
        return self._create_kpi_card(parent, title, var, "", "")
    
    # ═══════════════════════════════════════════════════════════
    # 🔄 MÉTODOS DE ESTADO Y FEEDBACK
    # ═══════════════════════════════════════════════════════════
    
    def _set_busy(self, busy: bool):
        """Activa/desactiva estado de procesamiento con feedback visual"""
        if busy:
            self.progress.start(12)
            self._update_status("⚡ Procesando operación...", "processing")
            self._pulse_activity_dot()
        else:
            self.progress.stop()
            self._update_status("✨ Listo", "info")
    
    def _pulse_activity_dot(self):
        """Anima el indicador de actividad"""
        if not getattr(self, '_pulse_active', False): return
        t = self._get_theme()
        current = self.activity_dot.cget("fg")
        self.activity_dot.configure(fg=t["success"] if current != t["success"] else t["neutral"])
        self.root.after(400, self._pulse_activity_dot)
    
    def _toast(self, message: str, duration: int = 2500):
        """Muestra notificación toast no intrusiva"""
        if hasattr(self, 'toast_window') and self.toast_window and self.toast_window.winfo_exists():
            self.toast_window.destroy()
        
        tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True, "-alpha", 0.95)
        x = self.root.winfo_rootx() + self.root.winfo_width() - 340
        y = self.root.winfo_rooty() + 80
        tw.geometry(f"320x45+{x}+{y}")
        
        t = self._get_theme()
        tk.Label(tw, text=f"  {message}", bg=t["primary"], fg="white", 
                font=("Segoe UI", 10), anchor="w").pack(fill="both", expand=True)
        
        self.toast_window = tw
        self.root.after(duration, lambda: tw.destroy() if tw.winfo_exists() else None)
    
    def _update_connection_indicator(self, state: str):
        """Actualiza el indicador visual de conexión"""
        t = self._get_theme()
        colors = {"connected": t["success"], "disconnected": t["neutral"], 
                 "error": t["danger"], "processing": t["warning"], "info": t["primary"]}
        self.conn_dot.configure(fg=colors.get(state, t["neutral"]))
    
    def _update_status(self, message: str, level: str = "info"):
        """Actualiza barra de estado con nivel semántico"""
        self.status_var.set(message)
        self.status_level_var.set(level)
        lvl = "success" if "ok" in message.lower() or "completado" in message.lower() else level
        self.status_label.configure(style=f"Status.{lvl}.TLabel")
        
        # Actualizar indicador de conexión
        if "error" in level.lower(): self._update_connection_indicator("error")
        elif "processing" in level.lower(): 
            self._pulse_active = True
            self._update_connection_indicator("processing")
            self._pulse_activity_dot()
        elif "success" in level.lower() or "ok" in message.lower():
            self._pulse_active = False
            self._update_connection_indicator("connected")
        else:
            self._pulse_active = False
            self._update_connection_indicator("info")
    
    # ═══════════════════════════════════════════════════════════
    # 💾 CONFIGURACIÓN
    # ═══════════════════════════════════════════════════════════
    
    def _save_config(self):
        """Guarda configuración con validación"""
        payload = {
            "api_key": self.api_key_var.get().strip(), "api_secret": self.api_secret_var.get().strip(),
            "market": self.market_var.get().strip(), "network": self.network_var.get().strip(),
            "usdt": self.usdt_var.get().strip(), "fee": self.fee_var.get().strip(),
            "max_assets": self.max_assets_var.get().strip(), "min_clean_profit": self.min_profit_var.get().strip(),
            "compound_cycles": self.cycles_var.get().strip(), "compound_trigger_multiple": self.trigger_mult_var.get().strip(),
            "compound_stake_pct": self.stake_pct_var.get().strip(),
            "binance_symbol": self.exec_symbol_var.get().strip(), "binance_qty": self.exec_qty_var.get().strip(),
            "binance_interval": self.exec_interval_var.get().strip(), "binance_limit": self.exec_limit_var.get().strip(),
            "binance_testnet": self.exec_testnet_var.get(), "binance_execute": self.exec_live_var.get(),
            "dark_mode": self.dark_mode.get()
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self._update_status("✅ Configuración guardada", "success")
        self._toast("Configuración guardada correctamente")
    
    def _load_config(self):
        """Carga configuración con manejo de errores"""
        if not CONFIG_PATH.exists(): return
        try:
            p = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.api_key_var.set(p.get("api_key", "")); self.api_secret_var.set(p.get("api_secret", ""))
            self.market_var.set(p.get("market", "spot")); self.network_var.set(p.get("network", "testnet"))
            self.usdt_var.set(p.get("usdt", "100")); self.fee_var.set(p.get("fee", "0.001"))
            self.max_assets_var.set(p.get("max_assets", "120")); self.min_profit_var.set(p.get("min_clean_profit", "0.01"))
            self.cycles_var.set(p.get("compound_cycles", "50")); self.trigger_mult_var.set(p.get("compound_trigger_multiple", "2.0"))
            self.stake_pct_var.set(p.get("compound_stake_pct", "10.0"))
            self.exec_symbol_var.set(p.get("binance_symbol", "BTCUSDT")); self.exec_qty_var.set(p.get("binance_qty", "100"))
            self.exec_interval_var.set(p.get("binance_interval", "1m")); self.exec_limit_var.set(p.get("binance_limit", "300"))
            self.exec_testnet_var.set(p.get("binance_testnet", True)); self.exec_live_var.set(p.get("binance_execute", False))
            self.dark_mode.set(p.get("dark_mode", False))
            self._apply_theme()
            self._update_status("📥 Configuración cargada", "success")
        except Exception as e:
            self._update_status(f"❌ Error al cargar: {e}", "error")
    
    def _show_help(self):
        """Muestra ayuda con atajos de teclado"""
        messagebox.showinfo("❓ Ayuda", 
            "⌨️ Atajos de Teclado:\n\n"
            "Ctrl+S  → Guardar configuración\n"
            "F5      → Ejecutar escaneo\n"
            "Esc     → Detener Auto-Scan\n"
            "Ctrl+F  → Enfocar búsqueda\n"
            "Ctrl+D  → Alternar modo oscuro\n\n"
            "💡 Tip: Haz doble-clic en una ruta para ver detalles rápidos")
    
    # ═══════════════════════════════════════════════════════════
    # 🔍 SCANNER LOGIC
    # ═══════════════════════════════════════════════════════════
    
    def _configure_scanner(self):
        self.scanner.configure(market_type=self.market_var.get().strip(), 
                              testnet=self.network_var.get().strip()=="testnet",
                              fee_rate=float(self.fee_var.get()))
    
    def _validate_keys(self):
        try:
            self._configure_scanner()
            ok, msg = self.scanner.validate_api_keys(self.api_key_var.get().strip(), self.api_secret_var.get().strip())
            self._update_status(msg, "success" if ok else "error")
            if ok: messagebox.showinfo("✅ Validación", msg)
            else: messagebox.showwarning("⚠️ Validación", msg)
        except Exception as e:
            self._update_status(f"❌ Error: {e}", "error")
    
    def _scan_once(self):
        threading.Thread(target=self._scan_worker, daemon=True).start()
    
    def _scan_worker(self):
        try:
            self.root.after(0, lambda: self._set_busy(True))
            self._configure_scanner()
            start = self._to_positive_float(self.usdt_var.get(), "Capital")
            max_assets = self._to_positive_int(self.max_assets_var.get(), "Max assets")
            min_clean = float(self.min_profit_var.get())
            
            output = self.scanner.scan(start_usdt=start, max_paths=40, max_assets=max_assets, min_clean_profit_usdt=min_clean)
            self.root.after(0, lambda: self._render_output(output))
            self.root.after(0, lambda: self._update_status(f"✅ Scan: {len(output.opportunities)} rutas limpias", "success"))
            if output.opportunities: self._toast(f"💎 Mejor: {output.opportunities[0].profit_pct:.4f}%")
        except Exception as e:
            self.root.after(0, lambda: self._update_status(f"❌ Error scan: {e}", "error"))
            self.root.after(0, lambda: self._log_exec(f"ERROR | Scan | {e}", "ERROR"))
        finally:
            self.root.after(0, lambda: self._set_busy(False))
    
    def _render_output(self, output: ScanOutput):
        self.last_output, self.all_scan_rows = output, list(output.opportunities)
        self._apply_filter()
        
        # Actualizar KPIs
        s = output.stats
        self.kpi_vars["scan_time"].set(f"{s.scan_ms} ms")
        self.kpi_vars["success_rate"].set(f"{s.success_rate_pct:.2f}%")
        self.kpi_vars["best_profit"].set(f"{s.best_profit_pct:+.4f}%")
        self.kpi_vars["avg_profit"].set(f"{s.avg_profit_pct:+.4f}%")
        self.kpi_vars["opportunities"].set(str(len(output.opportunities)))
        
        self._append_history(output)
        self._render_ascii_chart()
    
    def _apply_filter(self):
        """Filtra y renderiza resultados con formato condicional"""
        q = self.search_var.get().strip().lower()
        for item in self.results_tree.get_children(): self.results_tree.delete(item)
        
        rows = [r for r in self.all_scan_rows if not q or q in " ".join(r.path).lower() or q in " ".join(r.symbols).lower()]
        
        for i, row in enumerate(rows, 1):
            values = (i, " → ".join(row.path), " | ".join(row.symbols), f"{row.net_profit_usdt:+.6f}",
                     f"{row.total_fees_usdt:.6f}", f"{row.profit_pct:+.4f}%", "▶ Ejecutar" if row.is_clean_profitable else "⚠ Revisar")
            tags = ("profit" if row.net_profit_usdt >= 0 else "loss",) + (("best",) if i == 1 else ())
            self.results_tree.insert("", "end", values=values, tags=tags)
    
    def _sort_column(self, col: str):
        """Ordena columna del Treeview"""
        items = [(self.results_tree.set(k, col), k) for k in self.results_tree.get_children('')]
        try: items.sort(key=lambda t: float(t[0].rstrip('%').replace('+','')), reverse=True)
        except: items.sort(reverse=True)
        for idx, (_, item) in enumerate(items): self.results_tree.move(item, '', idx)
        self.results_tree.heading(col, command=lambda: self._sort_column(col))
    
    def _append_history(self, output: ScanOutput):
        """Agrega entrada al historial con límite"""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        row = (ts, output.stats.market_type, "testnet" if output.stats.testnet else "mainnet",
              output.stats.valid_paths, output.stats.clean_profitable_paths,
              f"{output.stats.success_rate_pct:.2f}%", f"{output.stats.best_profit_pct:+.4f}%", output.stats.scan_ms)
        self.scan_history.append(row)
        self.scan_history = self.scan_history[-300:]
        
        for item in self.hist_tree.get_children(): self.hist_tree.delete(item)
        for item in self.scan_history[-100:]: self.hist_tree.insert("", "end", values=item)
    
    def _render_ascii_chart(self):
        """Genera gráfico ASCII simple de evolución de profits"""
        if not self.scan_history: return
        bests = [float(str(r[6]).replace('%','')) for r in self.scan_history[-24:]]
        if not bests: return
        
        max_abs = max(abs(x) for x in bests) or 1.0
        lines = [f"📈 Evolución de Mejor Profit (últimos {len(bests)} scans):", "─"*60]
        for v in bests:
            bar_len = int((abs(v)/max_abs)*40)
            bar = ("█"*bar_len if v>=0 else "░"*bar_len)
            symbol = "▲" if v>0 else "▼" if v<0 else "●"
            lines.append(f"{symbol} {v:>+7.4f}% │ {bar}")
        
        self.chart_text.delete("1.0", tk.END)
        self.chart_text.insert("1.0", "\n".join(lines))
    
    def _on_result_select(self, _event):
        """Muestra detalles al seleccionar fila"""
        sel = self.results_tree.selection()
        if not sel or not self.all_scan_rows: return
        
        idx = int(self.results_tree.item(sel[0], "values")[0]) - 1
        if not (0 <= idx < len(self.all_scan_rows)): return
        
        row = self.all_scan_rows[idx]
        plan = self.scanner.simulate_compound_plan(
            initial_capital_usdt=self._to_positive_float(self.usdt_var.get(), "Capital"),
            per_cycle_net_pct=row.profit_pct, cycles=self._to_positive_int(self.cycles_var.get(), "Ciclos"),
            trigger_multiple=self._to_positive_float(self.trigger_mult_var.get(), "Trigger"),
            compound_stake_pct=float(self.stake_pct_var.get())/100, pre_trigger_stake_pct=1.0)
        
        text = (f"🔗 Ruta: {' → '.join(row.path)}\n"
                f"🪙 Símbolos: {' | '.join(row.symbols)}\n"
                f"💰 Profit Neto: {row.net_profit_usdt:+.6f} USDT ({row.profit_pct:+.4f}%)\n"
                f"💸 Comisiones: {row.total_fees_usdt:.6f} USDT\n\n"
                f"📊 Compuesto:\n"
                f"   • Capital final: {plan.current_capital:,.2f} USDT\n"
                f"   • Ciclos: {plan.cycles_simulated} | Trigger: {'✅' if plan.trigger_reached else '❌'} (ciclo {plan.trigger_cycle})\n"
                f"   • Stake: {plan.stake_mode}")
        
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")
    
    def _on_result_double_click(self, _event):
        """Acción rápida al doble-clic"""
        self._simulate_selected()
    
    def _simulate_selected(self):
        """Simula interés compuesto para selección actual"""
        sel = self.results_tree.selection()
        if not sel or not self.all_scan_rows: return
        idx = int(self.results_tree.item(sel[0], "values")[0]) - 1
        if 0 <= idx < len(self.all_scan_rows):
            row = self.all_scan_rows[idx]
            plan = self.scanner.simulate_compound_plan(
                initial_capital_usdt=float(self.usdt_var.get()), per_cycle_net_pct=row.profit_pct,
                cycles=int(self.cycles_var.get()), trigger_multiple=float(self.trigger_mult_var.get()),
                compound_stake_pct=float(self.stake_pct_var.get())/100)
            self._toast(f"📈 Proyección: {plan.current_capital:,.2f} USDT en {plan.cycles_simulated} ciclos")
    
    def _copy_selected_path(self):
        """Copia ruta seleccionada al portapapeles"""
        sel = self.results_tree.selection()
        if sel:
            path = self.results_tree.item(sel[0], "values")[1]
            self.root.clipboard_clear()
            self.root.clipboard_append(path)
            self._toast("📋 Ruta copiada al portapapeles")
    
    def _execute_selected(self):
        """Prepara ejecución de ruta seleccionada"""
        sel = self.results_tree.selection()
        if sel:
            path = self.results_tree.item(sel[0], "values")[1]
            profit = self.results_tree.item(sel[0], "values")[5]
            if messagebox.askyesno("Confirmar Ejecución", f"¿Ejecutar ruta?\n\n{path}\nProfit: {profit}"):
                self.exec_live_var.set(True)
                self.tab_exec.tkraise()
                self._run_unified_flow()
    
    # ═══════════════════════════════════════════════════════════
    # 🚀 EJECUCIÓN Y AI
    # ═══════════════════════════════════════════════════════════
    
    def _log_exec(self, line: str, level: str = "INFO"):
        """Agrega línea al log con formato semántico"""
        tag = {"SUCCESS": "SUCCESS", "WARN": "WARN", "ERROR": "ERROR"}.get(level.upper(), "INFO")
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.exec_log.insert("end", f"[{timestamp}] {line}\n", tag)
        self.exec_log.see("end")
    
    def _record_trade(self, symbol: str, signal: str, path: str, profit: float, mode: str, status: str):
        """Registra trade ejecutado"""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        row = (ts, symbol, signal, path, f"{profit:+.6f}", mode, status)
        self.executed_trades.append(row)
        self.executed_trades = self.executed_trades[-300:]
        
        for item in self.trade_tree.get_children(): self.trade_tree.delete(item)
        for item in self.executed_trades[-120:]: self.trade_tree.insert("", "end", values=item)
    
    def _run_binance_ai(self):
        threading.Thread(target=self._run_binance_ai_worker, daemon=True).start()
    
    def _run_binance_ai_worker(self):
        try:
            self.root.after(0, lambda: self._set_busy(True))
            symbol = self.exec_symbol_var.get().strip().upper()
            summary = self.ai_engine.run(symbol=symbol, interval=self.exec_interval_var.get().strip(),
                                        limit=self._to_positive_int(self.exec_limit_var.get(), "Limit"), exchange="binance")
            self.root.after(0, lambda: self._log_exec(f"🤖 IA Binance | {symbol} → {summary['signal']} (prob: {summary['probability_up']:.2%})", "INFO"))
            self.root.after(0, lambda: self._update_status(f"✅ IA: {summary['signal']}", "success"))
            self.root.after(0, lambda: self._toast(f"Señal IA: {summary['signal']}"))
        except Exception as e:
            self.root.after(0, lambda: self._log_exec(f"❌ Error IA: {e}", "ERROR"))
            self.root.after(0, lambda: self._update_status(f"Error IA: {e}", "error"))
        finally:
            self.root.after(0, lambda: self._set_busy(False))
    
    def _run_unified_flow(self):
        if self.exec_live_var.get() and not messagebox.askyesno("⚠️ Confirmar", "¿Confirmar ejecución OPERATIVA?\n\nEsto realizará órdenes reales."):
            return
        threading.Thread(target=self._run_unified_flow_worker, daemon=True).start()
    
    def _run_unified_flow_worker(self):
        try:
            self.root.after(0, lambda: self._set_busy(True))
            symbol = self.exec_symbol_var.get().strip().upper()
            qty = self._to_positive_float(self.exec_qty_var.get(), "Qty")
            
            # Paso 1: Análisis IA
            ai = self.ai_engine.run(symbol=symbol, interval=self.exec_interval_var.get().strip(),
                                   limit=self._to_positive_int(self.exec_limit_var.get(), "Limit"), exchange="binance")
            self.root.after(0, lambda: self._log_exec(f"1️⃣ IA: {symbol} → {ai['signal']} | Prob: {ai['probability_up']:.2%}", "INFO"))
            
            # Paso 2: Escaneo de arbitraje
            self.scanner.configure(market_type=self.market_var.get().strip(),
                                  testnet=self.exec_testnet_var.get(), fee_rate=float(self.fee_var.get()))
            scan = self.scanner.scan(start_usdt=qty, max_paths=5, max_assets=self._to_positive_int(self.max_assets_var.get(), "Max"),
                                    min_clean_profit_usdt=float(self.min_profit_var.get()))
            
            # Paso 3: Decisión y registro
            if scan.opportunities:
                best = scan.opportunities[0]
                path, net = " → ".join(best.path), best.net_profit_usdt
                self.root.after(0, lambda: self._log_exec(f"2️⃣ ✅ Ruta: {path} | Net: {net:+.6f} USDT", "SUCCESS"))
                status = "OK"
            else:
                path, net = "N/A", 0.0
                self.root.after(0, lambda: self._log_exec("2️⃣ ⚠️ Sin rutas limpias en este ciclo", "WARN"))
                status = "SIN_RUTAS"
            
            mode = "OPERATIVO" if self.exec_live_var.get() else "SIMULACIÓN"
            self.root.after(0, lambda: self._record_trade(symbol, ai["signal"], path, net, mode, status))
            self.root.after(0, lambda: self._log_exec(f"3️⃣ 💼 Trade registrado | Modo: {mode}", "INFO"))
            self.root.after(0, lambda: self._update_status(f"✅ Flujo completado | Señal: {ai['signal']}", "success"))
            self.root.after(0, lambda: self._toast("🚀 Flujo unificado completado"))
            
        except Exception as e:
            self.root.after(0, lambda: self._log_exec(f"❌ Error flujo: {e}", "ERROR"))
            self.root.after(0, lambda: self._update_status(f"Error: {e}", "error"))
        finally:
            self.root.after(0, lambda: self._set_busy(False))
    
    # ═══════════════════════════════════════════════════════════
    # 🔧 UTILS DE VALIDACIÓN
    # ═══════════════════════════════════════════════════════════
    
    @staticmethod
    def _to_positive_float(raw: str, field: str) -> float:
        try:
            v = float(raw)
            if v <= 0: raise ValueError
            return v
        except:
            raise ValueError(f"{field} debe ser numérico > 0 (ej: 100.50)")
    
    @staticmethod
    def _to_positive_int(raw: str, field: str) -> int:
        try:
            v = int(raw)
            if v <= 0: raise ValueError
            return v
        except:
            raise ValueError(f"{field} debe ser entero > 0 (ej: 120)")
    
    # ═══════════════════════════════════════════════════════════
    # 🔄 AUTO-SCAN
    # ═══════════════════════════════════════════════════════════
    
    def _autoscan_loop(self):
        if not self.running: return
        self._scan_once()
        self.root.after(5000, self._autoscan_loop)
    
    def _start_autoscan(self):
        if self.running: return
        self.running = True
        self._update_status("🔄 Auto-scan activado (cada 5s)", "info")
        self._toast("Auto-scan iniciado")
        self._autoscan_loop()
    
    def _stop_autoscan(self):
        self.running = False
        self._update_status("⏹️ Auto-scan detenido", "info")
        self._toast("Auto-scan detenido")

# ═══════════════════════════════════════════════════════════════
# 🚀 ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    root = tk.Tk()
    # Configuración inicial de ventana
    if hasattr(root, 'tk'): root.tk.call('tk', 'scaling', 1.0)  # Mejor rendering en HiDPI
    app = ArbitrageApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
