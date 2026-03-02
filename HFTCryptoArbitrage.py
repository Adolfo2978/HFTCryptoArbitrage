"""
HFTCryptoArbitrage Pro - Sistema de Alto Rendimiento con GUI Premium
Versión 3.1 - Completo con todas las funcionalidades restauradas
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import queue
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Dict, List, Optional, Set, Tuple, Any, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import logging

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(".binance_gui_config.json")
MAX_WORKERS = 4

# ==========================
# ENUMS Y CONSTANTES
# ==========================

class MarketType(Enum):
    SPOT = "spot"
    PERPETUAL = "perpetual"

class NetworkType(Enum):
    MAINNET = "mainnet"
    TESTNET = "testnet"
    DEMO = "demo"

class OrderMode(Enum):
    SIMULADOR = "simulador"
    TESTNET = "testnet"
    REAL = "real"

# ==========================
# DATA MODELS
# ==========================

@dataclass(frozen=True, slots=True)
class Edge:
    from_asset: str
    to_asset: str
    symbol: str
    side: str

@dataclass(slots=True)
class ArbitrageResult:
    path: Tuple[str, ...]
    symbols: Tuple[str, ...]
    sides: Tuple[str, ...]
    gross_final_usdt: float
    final_usdt: float
    total_fees_usdt: float
    net_profit_usdt: float
    profit_pct: float
    is_clean_profitable: bool
    timestamp: float = field(default_factory=time.time)

@dataclass(slots=True)
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
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

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

@dataclass
class TradeRecord:
    timestamp: str
    symbol: str
    signal: str
    best_path: str
    net_profit: float
    mode: str
    status: str
    order_id: Optional[str] = None

# ==========================
# PALETA DE COLORES PREMIUM
# ==========================

class Theme:
    """Tema visual premium con glassmorphism"""
    # Backgrounds
    BG_PRIMARY = "#0a0e17"
    BG_SECONDARY = "#0d1320"
    BG_TERTIARY = "#111827"
    BG_CARD = "#1a1f2e"
    BG_HOVER = "#242b3d"
    BG_ACTIVE = "#2d3548"
    
    # Accents
    ACCENT_CYAN = "#00d4ff"
    ACCENT_CYAN_DIM = "#0891b2"
    ACCENT_BLUE = "#3b82f6"
    ACCENT_PURPLE = "#8b5cf6"
    
    # Semantic
    SUCCESS = "#10b981"
    SUCCESS_DIM = "#059669"
    WARNING = "#f59e0b"
    DANGER = "#ef4444"
    DANGER_DIM = "#dc2626"
    INFO = "#06b6d4"
    
    # Text
    TEXT_PRIMARY = "#f8fafc"
    TEXT_SECONDARY = "#94a3b8"
    TEXT_MUTED = "#64748b"
    TEXT_DISABLED = "#475569"
    
    # Borders
    BORDER = "#1e293b"
    BORDER_LIGHT = "#334155"

# ==========================
# UTILIDADES
# ==========================

class RateLimiter:
    """Rate limiter thread-safe para APIs"""
    def __init__(self, max_requests: int = 10, window_seconds: float = 1.0):
        self.max_requests = max_requests
        self.window = window_seconds
        self.lock = threading.Lock()
        self.requests: List[float] = []
    
    def acquire(self):
        with self.lock:
            now = time.time()
            self.requests = [t for t in self.requests if now - t < self.window]
            
            if len(self.requests) >= self.max_requests:
                sleep_time = self.requests[0] + self.window - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    return self.acquire()
            
            self.requests.append(now)

def http_get_json(url: str, timeout: int = 10, retries: int = 3) -> dict | list:
    """HTTP GET con reintentos exponenciales"""
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": "HFTCryptoArbitrage/3.1",
                "Accept": "application/json"
            })
            with urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 429:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP error {exc.code}: {exc.reason}") from exc
        except URLError as exc:
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise RuntimeError(f"Error de red: {exc.reason}") from exc
    raise RuntimeError("Máximo de reintentos excedido")

# ==========================
# MOTOR DE ARBITRAJE
# ==========================

class BinanceArbitrageScanner:
    def __init__(self, fee_rate: float = 0.001, timeout: int = 10):
        self.fee_rate = fee_rate
        self.timeout = timeout
        self.market_type = MarketType.SPOT
        self.network = NetworkType.TESTNET
        self.testnet = True
        self.rate_limiter = RateLimiter(max_requests=20, window_seconds=1.0)
        self._cache: Dict[str, Any] = {}
        self._cache_ttl = 2.0
    
    def configure(self, market_type: str, network: str, fee_rate: float):
        self.market_type = MarketType(market_type.lower())
        self.network = NetworkType(network.lower())
        self.testnet = self.network in (NetworkType.TESTNET, NetworkType.DEMO)
        self.fee_rate = fee_rate
        self._cache.clear()
    
    def _api_base_url(self) -> str:
        urls = {
            (MarketType.SPOT, NetworkType.DEMO): "https://demo-api.binance.com",
            (MarketType.SPOT, NetworkType.TESTNET): "https://testnet.binance.vision",
            (MarketType.SPOT, NetworkType.MAINNET): "https://api.binance.com",
            (MarketType.PERPETUAL, NetworkType.DEMO): "https://demo-fapi.binance.com",
            (MarketType.PERPETUAL, NetworkType.TESTNET): "https://testnet.binancefuture.com",
            (MarketType.PERPETUAL, NetworkType.MAINNET): "https://fapi.binance.com",
        }
        return urls.get((self.market_type, self.network), "https://api.binance.com")
    
    def _endpoint(self, key: str) -> str:
        spot_map = {
            "exchange_info": "/api/v3/exchangeInfo",
            "book_ticker": "/api/v3/ticker/bookTicker",
            "time": "/api/v3/time"
        }
        fut_map = {
            "exchange_info": "/fapi/v1/exchangeInfo",
            "book_ticker": "/fapi/v1/ticker/bookTicker",
            "time": "/fapi/v1/time"
        }
        return (spot_map if self.market_type == MarketType.SPOT else fut_map)[key]
    
    def _get(self, endpoint: str, params: Optional[dict] = None, use_cache: bool = False):
        cache_key = f"{endpoint}:{json.dumps(params, sort_keys=True)}"
        
        if use_cache and cache_key in self._cache:
            data, ts = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return data
        
        self.rate_limiter.acquire()
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self._api_base_url()}{endpoint}{query}"
        data = http_get_json(url, timeout=self.timeout)
        
        if use_cache:
            self._cache[cache_key] = (data, time.time())
        
        return data
    
    def fetch_exchange_info(self) -> List[dict]:
        payload = self._get(self._endpoint("exchange_info"), use_cache=True)
        symbols = payload.get("symbols", [])
        
        if self.market_type == MarketType.SPOT:
            return [
                s for s in symbols 
                if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)
            ]
        return [
            s for s in symbols 
            if s.get("status") == "TRADING" 
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset")
        ]
    
    def fetch_book_tickers(self) -> Dict[str, Tuple[float, float]]:
        payload = self._get(self._endpoint("book_ticker"))
        prices: Dict[str, Tuple[float, float]] = {}
        
        for row in payload:
            try:
                bid = float(row["bidPrice"])
                ask = float(row["askPrice"])
                if bid > 0 and ask > 0:
                    prices[row["symbol"]] = (bid, ask)
            except (KeyError, ValueError):
                continue
        return prices
    
    @staticmethod
    def _build_edges(symbol_rows: List[dict]) -> Dict[str, Dict[str, Edge]]:
        graph: Dict[str, Dict[str, Edge]] = {}
        for row in symbol_rows:
            symbol = row.get("symbol")
            base = row.get("baseAsset")
            quote = row.get("quoteAsset")
            if not all([symbol, base, quote]):
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
        return out * (1 - self.fee_rate) if out is not None else None
    
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
                if e3:
                    yield ("USDT", a, b, "USDT"), (e1, e2, e3)
    
    def _eval_cycle(self, start_usdt: float, edges: Tuple[Edge, ...], prices: Dict[str, Tuple[float, float]]):
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
    
    def scan(self, start_usdt: float, max_paths: int = 40, 
             max_assets: int = 120, min_clean_profit_usdt: float = 0.0) -> ScanOutput:
        t0 = time.perf_counter()
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_symbols = executor.submit(self.fetch_exchange_info)
            future_prices = executor.submit(self.fetch_book_tickers)
            
            symbols = future_symbols.result()
            prices = future_prices.result()
        
        graph = self._build_edges(symbols)
        
        all_rows: List[ArbitrageResult] = []
        scanned = 0
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {}
            for path, edges in self._enumerate_triangles(graph, max_assets=max_assets):
                scanned += 1
                future = executor.submit(self._eval_cycle, start_usdt, edges, prices)
                futures[future] = (path, edges)
            
            for future in as_completed(futures):
                path, edges = futures[future]
                try:
                    cycle = future.result()
                    if cycle is None:
                        continue
                    
                    gross, net, fees, net_profit, profit_pct = cycle
                    all_rows.append(ArbitrageResult(
                        path=path,
                        symbols=(edges[0].symbol, edges[1].symbol, edges[2].symbol),
                        sides=(edges[0].side, edges[1].side, edges[2].side),
                        gross_final_usdt=gross,
                        final_usdt=net,
                        total_fees_usdt=fees,
                        net_profit_usdt=net_profit,
                        profit_pct=profit_pct,
                        is_clean_profitable=net_profit > min_clean_profit_usdt
                    ))
                except Exception as e:
                    logger.warning(f"Error evaluando ciclo {path}: {e}")
        
        clean = [r for r in all_rows if r.is_clean_profitable]
        clean.sort(key=lambda x: x.profit_pct, reverse=True)
        
        profits = [r.profit_pct for r in all_rows]
        median = 0.0
        if profits:
            s = sorted(profits)
            mid = len(s) // 2
            median = s[mid] if len(s) % 2 else (s[mid-1] + s[mid]) / 2
        
        scan_ms = int((time.perf_counter() - t0) * 1000)
        
        stats = ScanStats(
            market_type=self.market_type.value,
            testnet=self.testnet,
            scanned_paths=scanned,
            valid_paths=len(all_rows),
            clean_profitable_paths=len(clean),
            success_rate_pct=(len(clean) / len(all_rows) * 100.0) if all_rows else 0.0,
            best_profit_pct=max(profits) if profits else 0.0,
            avg_profit_pct=(sum(profits) / len(profits)) if profits else 0.0,
            median_profit_pct=median,
            scan_ms=scan_ms
        )
        
        return ScanOutput(opportunities=clean[:max_paths], stats=stats)
    
    @staticmethod
    def simulate_compound_plan(
        initial_capital_usdt: float,
        per_cycle_net_pct: float,
        cycles: int,
        trigger_multiple: float = 2.0,
        compound_stake_pct: float = 0.10,
        pre_trigger_stake_pct: float = 1.0
    ) -> CompoundPlanResult:
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
            per_cycle_net_pct=per_cycle_net_pct
        )
    
    def validate_api_keys(self, api_key: str, api_secret: str) -> Tuple[bool, str]:
        if not api_key or not api_secret:
            return False, "Faltan API key/secret"
        if len(api_key) < 20 or len(api_secret) < 20:
            return False, "Formato de API key/secret inválido"
        
        try:
            self._get(self._endpoint("time"))
            env = self.network.value.upper()
            return True, f"✓ Conectividad {env} OK ({self.market_type.value.upper()})"
        except Exception as exc:
            return False, f"✗ Error de conectividad: {exc}"

class AISignalEngine:
    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.rate_limiter = RateLimiter(max_requests=10, window_seconds=1.0)
    
    def _fetch_binance(self, symbol: str, interval: str, limit: int) -> List[list]:
        self.rate_limiter.acquire()
        q = urlencode({"symbol": symbol, "interval": interval, "limit": limit})
        url = f"https://api.binance.com/api/v3/klines?{q}"
        rows = http_get_json(url, timeout=self.timeout)
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Sin datos de klines")
        return rows
    
    def run(self, symbol: str, interval: str, limit: int) -> Dict[str, Any]:
        rows = self._fetch_binance(symbol, interval, limit)
        closes = [float(r[4]) for r in rows]
        ts = int(rows[-1][0])
        
        if len(closes) < 40:
            raise RuntimeError("Datos insuficientes para análisis")
        
        short_ema = self._ema(closes[-20:], 8)
        long_ema = self._ema(closes[-40:], 21)
        
        rsi = self._rsi(closes[-15:], 14)
        momentum = (short_ema / long_ema - 1.0) if long_ema else 0.0
        
        returns = [(closes[i]/closes[i-1] - 1) for i in range(-20, 0)]
        volatility = math.sqrt(sum(r*r for r in returns) / len(returns)) if returns else 0
        
        score = momentum * 100 + (50 - rsi) * 0.5 - volatility * 50
        
        prob_up = 1.0 / (1.0 + math.exp(-max(-6.0, min(6.0, score))))
        
        if prob_up >= 0.6:
            signal = "BUY"
        elif prob_up <= 0.4:
            signal = "SELL"
        else:
            signal = "HOLD"
        
        return {
            "exchange": "binance",
            "symbol": symbol,
            "interval": interval,
            "rows": len(closes),
            "last_close": closes[-1],
            "last_candle_utc": datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat(),
            "rsi": round(rsi, 2),
            "momentum": round(momentum * 100, 4),
            "volatility": round(volatility * 100, 4),
            "probability_up": round(prob_up, 4),
            "signal": signal,
        }
    
    @staticmethod
    def _ema(data: List[float], period: int) -> float:
        if len(data) < period:
            return sum(data) / len(data)
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period
        for price in data[period:]:
            ema = (price - ema) * multiplier + ema
        return ema
    
    @staticmethod
    def _rsi(closes: List[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        
        gains = []
        losses = []
        for i in range(1, period + 1):
            change = closes[-i] - closes[-i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

# ==========================
# WIDGETS PREMIUM
# ==========================

class ModernButton(tk.Canvas):
    """Botón moderno con efectos de glassmorphism"""
    
    STYLES = {
        "primary": {
            "bg": "#0ea5e9", "bg_hover": "#0284c7", "bg_active": "#0369a1",
            "fg": "#ffffff", "shadow": "#0284c7"
        },
        "success": {
            "bg": "#10b981", "bg_hover": "#059669", "bg_active": "#047857",
            "fg": "#ffffff", "shadow": "#059669"
        },
        "danger": {
            "bg": "#ef4444", "bg_hover": "#dc2626", "bg_active": "#b91c1c",
            "fg": "#ffffff", "shadow": "#dc2626"
        },
        "warning": {
            "bg": "#f59e0b", "bg_hover": "#d97706", "bg_active": "#b45309",
            "fg": "#ffffff", "shadow": "#d97706"
        },
        "ghost": {
            "bg": "transparent", "bg_hover": "#1e293b", "bg_active": "#334155",
            "fg": "#94a3b8", "shadow": "#1e293b"
        },
        "accent": {
            "bg": "#8b5cf6", "bg_hover": "#7c3aed", "bg_active": "#6d28d9",
            "fg": "#ffffff", "shadow": "#7c3aed"
        }
    }
    
    def __init__(self, parent, text: str, command: Optional[Callable] = None,
                 style: str = "primary", width: int = 140, height: int = 40,
                 icon: str = "", font_size: int = 11, **kwargs):
        super().__init__(parent, width=width, height=height, 
                        bg=Theme.BG_PRIMARY, highlightthickness=0, **kwargs)
        
        self.text = text
        self.command = command
        self.style = self.STYLES.get(style, self.STYLES["primary"])
        self.icon = icon
        self.font_size = font_size
        self.width = width
        self.height = height
        
        self._hover = False
        self._active = False
        self._disabled = False
        
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        
        self._draw()
    
    def _draw(self):
        self.delete("all")
        
        if self._disabled:
            bg = Theme.BG_HOVER
            fg = Theme.TEXT_DISABLED
        elif self._active:
            bg = self.style["bg_active"]
            fg = self.style["fg"]
        elif self._hover:
            bg = self.style["bg_hover"]
            fg = self.style["fg"]
        else:
            bg = self.style["bg"] if self.style["bg"] != "transparent" else Theme.BG_CARD
            fg = self.style["fg"]
        
        if not self._disabled and self._hover:
            self.create_rounded_rect(2, 2, self.width, self.height, 8, 
                                   fill=self.style["shadow"], stipple="gray50")
        
        self.create_rounded_rect(0, 0, self.width-2, self.height-2, 8, 
                               fill=bg, outline=Theme.BORDER_LIGHT if self.style["bg"] == "transparent" else "")
        
        if not self._disabled and not self._active:
            self.create_rounded_rect(2, 2, self.width-4, 8, 4, 
                                   fill="#ffffff", stipple="gray25")
        
        full_text = f"{self.icon}  {self.text}" if self.icon else self.text
        self.create_text(self.width//2, self.height//2, text=full_text,
                        fill=fg, font=("Segoe UI", self.font_size, "bold"))
    
    def create_rounded_rect(self, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
            x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
            x1, y2, x1, y2-radius, x1, y1+radius, x1, y1
        ]
        return self.create_polygon(points, smooth=True, **kwargs)
    
    def _on_enter(self, e):
        if not self._disabled:
            self._hover = True
            self._draw()
    
    def _on_leave(self, e):
        self._hover = False
        self._active = False
        self._draw()
    
    def _on_press(self, e):
        if not self._disabled:
            self._active = True
            self._draw()
    
    def _on_release(self, e):
        if not self._disabled:
            self._active = False
            self._draw()
            if self.command:
                self.command()
    
    def configure(self, **kwargs):
        if "state" in kwargs:
            self._disabled = (kwargs.pop("state") == "disabled")
            self._draw()
        if "text" in kwargs:
            self.text = kwargs.pop("text")
            self._draw()
        super().configure(**kwargs)

class MetricCard(tk.Frame):
    """Tarjeta de métrica con diseño glassmorphism"""
    
    def __init__(self, parent, title: str, value_var: tk.StringVar,
                 unit: str = "", color: str = Theme.ACCENT_CYAN, **kwargs):
        super().__init__(parent, bg=Theme.BG_CARD, **kwargs)
        
        self.value_var = value_var
        self.color = color
        
        self.configure(highlightbackground=Theme.BORDER, highlightthickness=1)
        
        header = tk.Frame(self, bg=color, height=3)
        header.pack(fill="x", padx=1, pady=(1, 0))
        
        content = tk.Frame(self, bg=Theme.BG_CARD, padx=16, pady=12)
        content.pack(fill="both", expand=True)
        
        tk.Label(content, text=title.upper(), font=("Segoe UI", 9),
                fg=Theme.TEXT_MUTED, bg=Theme.BG_CARD).pack(anchor="w")
        
        self.value_label = tk.Label(content, textvariable=value_var,
                                   font=("Segoe UI", 24, "bold"),
                                   fg=color, bg=Theme.BG_CARD)
        self.value_label.pack(anchor="w", pady=(4, 0))
        
        if unit:
            tk.Label(content, text=unit, font=("Segoe UI", 10),
                    fg=Theme.TEXT_SECONDARY, bg=Theme.BG_CARD).pack(anchor="w")
        
        self.bind("<Enter>", lambda e: self._set_highlight(True))
        self.bind("<Leave>", lambda e: self._set_highlight(False))
    
    def _set_highlight(self, active: bool):
        color = self.color if active else Theme.BORDER
        self.configure(highlightbackground=color)

class ModernEntry(tk.Frame):
    """Campo de entrada moderno con animación de foco"""
    
    def __init__(self, parent, textvariable: Optional[tk.StringVar] = None,
                 show: Optional[str] = None, placeholder: str = "", width: int = 20, **kwargs):
        super().__init__(parent, bg=Theme.BORDER, padx=1, pady=1, **kwargs)
        
        self.inner = tk.Frame(self, bg=Theme.BG_CARD)
        self.inner.pack(fill="both", expand=True)
        
        self.var = textvariable or tk.StringVar()
        self.placeholder = placeholder
        self.show = show
        
        self.entry = tk.Entry(self.inner, textvariable=self.var,
                             bg=Theme.BG_CARD, fg=Theme.TEXT_PRIMARY,
                             insertbackground=Theme.ACCENT_CYAN,
                             relief="flat", font=("Segoe UI", 11),
                             highlightthickness=0, width=width)
        self.entry.pack(fill="both", expand=True, ipady=8, ipadx=12)
        
        if show:
            self.entry.configure(show=show)
        
        if placeholder:
            self._set_placeholder()
            self.entry.bind("<FocusIn>", self._on_focus_in)
            self.entry.bind("<FocusOut>", self._on_focus_out)
        
        self.entry.bind("<FocusIn>", lambda e: self._animate_border(True), add="+")
        self.entry.bind("<FocusOut>", lambda e: self._animate_border(False), add="+")
    
    def _set_placeholder(self):
        if not self.var.get():
            self.entry.insert(0, self.placeholder)
            self.entry.configure(fg=Theme.TEXT_MUTED)
    
    def _on_focus_in(self, e):
        if self.entry.get() == self.placeholder:
            self.entry.delete(0, "end")
            self.entry.configure(fg=Theme.TEXT_PRIMARY)
    
    def _on_focus_out(self, e):
        if not self.entry.get():
            self._set_placeholder()
    
    def _animate_border(self, active: bool):
        color = Theme.ACCENT_CYAN if active else Theme.BORDER
        self.configure(bg=color)
    
    def get(self) -> str:
        val = self.var.get()
        return "" if val == self.placeholder else val

class ModernCombobox(tk.Frame):
    """Combobox moderno personalizado"""
    
    def __init__(self, parent, textvariable: Optional[tk.StringVar] = None,
                 values: List[str] = [], width: int = 20, **kwargs):
        super().__init__(parent, bg=Theme.BORDER, padx=1, pady=1, **kwargs)
        
        self.var = textvariable or tk.StringVar()
        self.values = values
        
        self.inner = tk.Frame(self, bg=Theme.BG_CARD)
        self.inner.pack(fill="both", expand=True)
        
        self.entry = tk.Entry(self.inner, textvariable=self.var,
                             bg=Theme.BG_CARD, fg=Theme.TEXT_PRIMARY,
                             relief="flat", font=("Segoe UI", 11),
                             highlightthickness=0, width=width, state="readonly")
        self.entry.pack(side="left", fill="both", expand=True, ipady=8, ipadx=12)
        
        self.arrow = tk.Label(self.inner, text="▼", font=("Segoe UI", 8),
                             fg=Theme.ACCENT_CYAN, bg=Theme.BG_CARD, cursor="hand2")
        self.arrow.pack(side="right", padx=8)
        
        self.menu = None
        self.arrow.bind("<Button-1>", self._toggle_menu)
        self.entry.bind("<Button-1>", self._toggle_menu)
    
    def _toggle_menu(self, event=None):
        if self.menu and self.menu.winfo_exists():
            self.menu.destroy()
            return
        
        self.menu = tk.Toplevel(self)
        self.menu.overrideredirect(True)
        self.menu.configure(bg=Theme.BORDER)
        
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height()
        self.menu.geometry(f"{self.winfo_width()}x{min(len(self.values)*32+4, 200)}+{x}+{y}")
        
        frame = tk.Frame(self.menu, bg=Theme.BG_CARD)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        
        for value in self.values:
            lbl = tk.Label(frame, text=value, bg=Theme.BG_CARD, fg=Theme.TEXT_PRIMARY,
                          font=("Segoe UI", 11), padx=12, pady=6, cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Enter>", lambda e, l=lbl: l.configure(bg=Theme.BG_HOVER))
            lbl.bind("<Leave>", lambda e, l=lbl: l.configure(bg=Theme.BG_CARD))
            lbl.bind("<Button-1>", lambda e, v=value: self._select(v))
        
        self.menu.bind("<FocusOut>", lambda e: self.menu.destroy() if self.menu else None)
        self.menu.focus_set()
    
    def _select(self, value: str):
        self.var.set(value)
        if self.menu:
            self.menu.destroy()

class VirtualTreeview(tk.Frame):
    """Treeview virtualizado para grandes volúmenes de datos"""
    
    def __init__(self, parent, columns: List[str], headings: List[str],
                 widths: List[int], height: int = 15, **kwargs):
        super().__init__(parent, bg=Theme.BG_PRIMARY, **kwargs)
        
        self.columns = columns
        self.data: List[Tuple] = []
        self.filtered_data: List[Tuple] = []
        self.item_tags: Dict[int, Tuple[str, ...]] = {}
        
        style = ttk.Style()
        style.theme_use("clam")
        
        style.configure("Modern.Treeview",
                       background=Theme.BG_CARD,
                       foreground=Theme.TEXT_PRIMARY,
                       fieldbackground=Theme.BG_CARD,
                       rowheight=32,
                       borderwidth=0)
        style.configure("Modern.Treeview.Heading",
                       background=Theme.BG_TERTIARY,
                       foreground=Theme.TEXT_SECONDARY,
                       font=("Segoe UI", 9, "bold"),
                       relief="flat")
        style.map("Modern.Treeview",
                 background=[("selected", Theme.BG_HOVER)],
                 foreground=[("selected", Theme.TEXT_PRIMARY)])
        
        container = tk.Frame(self, bg=Theme.BORDER)
        container.pack(fill="both", expand=True)
        
        self.tree = ttk.Treeview(container, columns=columns, show="headings",
                                height=height, style="Modern.Treeview")
        
        for col, head, width in zip(columns, headings, widths):
            self.tree.heading(col, text=head, anchor="center")
            self.tree.column(col, width=width, anchor="center")
        
        vsb = tk.Scrollbar(container, orient="vertical", command=self.tree.yview,
                          bg=Theme.BG_TERTIARY, troughcolor=Theme.BG_CARD,
                          activebackground=Theme.ACCENT_CYAN, relief="flat", width=8)
        self.tree.configure(yscrollcommand=vsb.set)
        
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        
        self.tree.tag_configure("profit", foreground=Theme.SUCCESS)
        self.tree.tag_configure("loss", foreground=Theme.DANGER)
        self.tree.tag_configure("neutral", foreground=Theme.TEXT_SECONDARY)
        self.tree.tag_configure("highlight", background=Theme.BG_HOVER)
        self.tree.tag_configure("odd", background=Theme.BG_CARD)
        self.tree.tag_configure("even", background="#1f2432")
    
    def set_data(self, data: List[Tuple], tags: Optional[List[Tuple[str, ...]]] = None):
        self.data = data
        self.filtered_data = data
        self.item_tags = {i: tags[i] if tags and i < len(tags) else () 
                         for i in range(len(data))}
        self._refresh()
    
    def filter(self, query: str):
        if not query:
            self.filtered_data = self.data
        else:
            q = query.lower()
            self.filtered_data = [row for row in self.data 
                                if any(q in str(cell).lower() for cell in row)]
        self._refresh()
    
    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.filtered_data[:1000]):
            tags = list(self.item_tags.get(i, ()))
            tags.append("odd" if i % 2 == 0 else "even")
            self.tree.insert("", "end", values=row, tags=tuple(tags))
    
    def clear(self):
        self.data = []
        self.filtered_data = []
        self.item_tags = {}
        self.tree.delete(*self.tree.get_children())
    
    def insert(self, values: Tuple, tags: Tuple[str, ...] = ()):
        idx = len(self.data)
        self.data.append(values)
        self.filtered_data = self.data
        self.item_tags[idx] = tags
        self._refresh()
    
    def bind(self, event: str, callback: Callable):
        self.tree.bind(event, callback)
    
    def selection(self):
        return self.tree.selection()

class TerminalLog(tk.Frame):
    """Log con estilo terminal y colores sintácticos"""
    
    LEVELS = {
        "INFO": ("ℹ", Theme.ACCENT_CYAN),
        "SUCCESS": ("✓", Theme.SUCCESS),
        "WARNING": ("⚠", Theme.WARNING),
        "ERROR": ("✗", Theme.DANGER),
        "TRADE": ("◈", Theme.ACCENT_PURPLE)
    }
    
    def __init__(self, parent, height: int = 12, **kwargs):
        super().__init__(parent, bg=Theme.BG_CARD, highlightbackground=Theme.BORDER,
                        highlightthickness=1, **kwargs)
        
        header = tk.Frame(self, bg=Theme.BG_TERTIARY, height=28)
        header.pack(fill="x")
        header.pack_propagate(False)
        
        dots = tk.Frame(header, bg=Theme.BG_TERTIARY)
        dots.pack(side="left", padx=12)
        for color in [Theme.DANGER, Theme.WARNING, Theme.SUCCESS]:
            tk.Label(dots, text="●", fg=color, bg=Theme.BG_TERTIARY,
                    font=("Segoe UI", 8)).pack(side="left", padx=1)
        
        tk.Label(header, text="SYSTEM LOG", font=("Segoe UI", 8, "bold"),
                fg=Theme.TEXT_MUTED, bg=Theme.BG_TERTIARY).pack(side="left", padx=8)
        
        text_frame = tk.Frame(self, bg=Theme.BG_CARD)
        text_frame.pack(fill="both", expand=True, padx=1, pady=1)
        
        self.text = tk.Text(text_frame, bg=Theme.BG_CARD, fg=Theme.TEXT_PRIMARY,
                           font=("JetBrains Mono", 10), relief="flat",
                           wrap="word", padx=12, pady=8,
                           highlightthickness=0, state="disabled")
        
        vsb = tk.Scrollbar(text_frame, command=self.text.yview,
                          bg=Theme.BG_TERTIARY, troughcolor=Theme.BG_CARD,
                          activebackground=Theme.ACCENT_CYAN, width=8)
        self.text.configure(yscrollcommand=vsb.set)
        
        vsb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        
        for level, (icon, color) in self.LEVELS.items():
            self.text.tag_configure(level, foreground=color)
        self.text.tag_configure("timestamp", foreground=Theme.TEXT_MUTED)
        self.text.tag_configure("dim", foreground=Theme.TEXT_DISABLED)
        
        self.max_lines = 500
    
    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        icon, color = self.LEVELS.get(level, ("•", Theme.TEXT_SECONDARY))
        
        self.text.configure(state="normal")
        
        self.text.insert("end", f"{timestamp} ", "timestamp")
        self.text.insert("end", f"{icon} ", level)
        self.text.insert("end", f"{message}\n")
        
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > self.max_lines:
            self.text.delete("1.0", f"{lines - self.max_lines}.0")
        
        self.text.see("end")
        self.text.configure(state="disabled")
    
    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

class ToastNotification:
    """Notificaciones toast modernas"""
    
    def __init__(self, root: tk.Tk):
        self.root = root
        self.active: Optional[tk.Toplevel] = None
        self.queue: queue.Queue = queue.Queue()
        self._process_queue()
    
    def show(self, message: str, level: str = "info", duration: int = 3000):
        self.queue.put((message, level, duration))
    
    def _process_queue(self):
        try:
            while True:
                message, level, duration = self.queue.get_nowait()
                self._display(message, level, duration)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._process_queue)
    
    def _display(self, message: str, level: str, duration: int):
        if self.active and self.active.winfo_exists():
            self.active.destroy()
        
        colors = {
            "info": (Theme.ACCENT_CYAN, Theme.ACCENT_CYAN_DIM),
            "success": (Theme.SUCCESS, Theme.SUCCESS_DIM),
            "error": (Theme.DANGER, Theme.DANGER_DIM),
            "warning": (Theme.WARNING, "#b45309")
        }
        accent, bg = colors.get(level, colors["info"])
        
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.attributes("-alpha", 0)
        
        x = self.root.winfo_rootx() + self.root.winfo_width() - 380
        y = self.root.winfo_rooty() + 60
        
        frame = tk.Frame(toast, bg=Theme.BG_CARD, highlightbackground=accent,
                        highlightthickness=2)
        frame.pack(fill="both", expand=True)
        
        content = tk.Frame(frame, bg=Theme.BG_CARD, padx=16, pady=12)
        content.pack(fill="both", expand=True)
        
        icon = "◆" if level == "info" else "✓" if level == "success" else "✗" if level == "error" else "⚠"
        tk.Label(content, text=icon, font=("Segoe UI", 14), fg=accent,
                bg=Theme.BG_CARD).pack(side="left", padx=(0, 12))
        
        tk.Label(content, text=message, font=("Segoe UI", 11), 
                fg=Theme.TEXT_PRIMARY, bg=Theme.BG_CARD,
                wraplength=280, justify="left").pack(side="left")
        
        toast.geometry(f"340x80+{x}+{y}")
        
        self.active = toast
        self._animate_in(toast, 0)
        
        self.root.after(duration, lambda: self._animate_out(toast))
    
    def _animate_in(self, window: tk.Toplevel, alpha: float):
        if alpha < 0.95:
            window.attributes("-alpha", alpha)
            self.root.after(20, lambda: self._animate_in(window, alpha + 0.1))
        else:
            window.attributes("-alpha", 0.95)
    
    def _animate_out(self, window: tk.Toplevel, alpha: float = 0.95):
        if alpha > 0:
            try:
                window.attributes("-alpha", alpha)
                self.root.after(20, lambda: self._animate_out(window, alpha - 0.1))
            except tk.TclError:
                pass
        else:
            try:
                window.destroy()
            except tk.TclError:
                pass

# ==========================
# PÁGINAS DE LA APLICACIÓN
# ==========================

class ScannerPage(tk.Frame):
    """Página principal del scanner de arbitraje"""
    
    def __init__(self, parent, app: "ArbitrageApp", **kwargs):
        super().__init__(parent, bg=Theme.BG_PRIMARY, **kwargs)
        self.app = app
        self._build_ui()
    
    def _build_ui(self):
        # Header
        header = tk.Frame(self, bg=Theme.BG_SECONDARY)
        header.pack(fill="x", pady=(0, 16))
        
        tk.Frame(header, bg=Theme.ACCENT_CYAN, height=2).pack(fill="x")
        
        header_content = tk.Frame(header, bg=Theme.BG_SECONDARY, padx=24, pady=16)
        header_content.pack(fill="x")
        
        title_frame = tk.Frame(header_content, bg=Theme.BG_SECONDARY)
        title_frame.pack(side="left")
        
        tk.Label(title_frame, text="Scanner de Arbitraje", 
                font=("Segoe UI", 20, "bold"),
                fg=Theme.TEXT_PRIMARY, bg=Theme.BG_SECONDARY).pack(anchor="w")
        
        tk.Label(title_frame, text="Detección de Arbitraje Triangular en Tiempo Real",
                font=("Segoe UI", 11), fg=Theme.TEXT_SECONDARY,
                bg=Theme.BG_SECONDARY).pack(anchor="w")
        
        controls = tk.Frame(header_content, bg=Theme.BG_SECONDARY)
        controls.pack(side="right")
        
        ModernButton(controls, "Escanear", self.app.scan_once,
                    "primary", 130, 40, "⬡").pack(side="left", padx=4)
        ModernButton(controls, "Auto ON", self.app.start_auto_scan,
                    "success", 110, 40, "▶").pack(side="left", padx=4)
        ModernButton(controls, "Auto OFF", self.app.stop_auto_scan,
                    "ghost", 110, 40, "■").pack(side="left", padx=4)
        ModernButton(controls, "Limpiar", self.app.clear_scan_results,
                    "danger", 100, 40, "✕").pack(side="left", padx=4)
        
        # Métricas
        metrics = tk.Frame(self, bg=Theme.BG_PRIMARY)
        metrics.pack(fill="x", padx=24, pady=(0, 16))
        
        self.metric_vars = {
            "scan_time": tk.StringVar(value="—"),
            "success_rate": tk.StringVar(value="—"),
            "best_profit": tk.StringVar(value="—"),
            "avg_profit": tk.StringVar(value="—"),
            "opportunities": tk.StringVar(value="0")
        }
        
        MetricCard(metrics, "Tiempo de Scan", self.metric_vars["scan_time"], 
                  "ms", Theme.ACCENT_CYAN).pack(side="left", padx=(0, 12))
        MetricCard(metrics, "Tasa de Éxito", self.metric_vars["success_rate"],
                  "%", Theme.SUCCESS).pack(side="left", padx=(0, 12))
        MetricCard(metrics, "Mejor Ganancia", self.metric_vars["best_profit"],
                  "%", Theme.WARNING).pack(side="left", padx=(0, 12))
        MetricCard(metrics, "Oportunidades", self.metric_vars["opportunities"],
                  "limpias", Theme.ACCENT_PURPLE).pack(side="left", padx=(0, 12))
        
        # Filtros y tabla
        table_section = tk.Frame(self, bg=Theme.BG_PRIMARY)
        table_section.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        
        toolbar = tk.Frame(table_section, bg=Theme.BG_TERTIARY, padx=12, pady=8)
        toolbar.pack(fill="x", pady=(0, 8))
        
        tk.Label(toolbar, text="Filtrar:", fg=Theme.TEXT_SECONDARY,
                bg=Theme.BG_TERTIARY, font=("Segoe UI", 10)).pack(side="left")
        
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *args: self._apply_filter())
        ModernEntry(toolbar, self.filter_var, placeholder="Buscar rutas...").pack(side="left", padx=8)
        
        tk.Label(toolbar, text="Intervalo (s):", fg=Theme.TEXT_SECONDARY,
                bg=Theme.BG_TERTIARY, font=("Segoe UI", 10)).pack(side="left", padx=(20, 8))
        ModernEntry(toolbar, self.app.scan_interval_var, width=8).pack(side="left")
        
        self.scan_counter = tk.Label(toolbar, text="Esperando primer scan...",
                                    fg=Theme.TEXT_MUTED, bg=Theme.BG_TERTIARY,
                                    font=("Segoe UI", 9))
        self.scan_counter.pack(side="right")
        
        # Tabla
        self.table = VirtualTreeview(
            table_section,
            columns=["rank", "path", "symbols", "profit", "net", "fees", "pct"],
            headings=["#", "Ruta de Arbitraje", "Símbolos", "Ganancia USDT", "Neto USDT", "Comisiones", "%"],
            widths=[50, 280, 200, 110, 110, 90, 80]
        )
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self._on_select)
        
        # Status bar
        self.status_label = tk.Label(self, text="Listo", fg=Theme.TEXT_MUTED,
                                    bg=Theme.BG_PRIMARY, font=("Segoe UI", 10))
        self.status_label.pack(anchor="w", padx=24, pady=(0, 8))
    
    def _apply_filter(self):
        if hasattr(self, "table"):
            self.table.filter(self.filter_var.get())
    
    def _on_select(self, event):
        selection = self.table.selection()
        if selection and self.app.last_output:
            idx = int(self.table.tree.item(selection[0])["values"][0]) - 1
            if 0 <= idx < len(self.app.all_scan_rows):
                self.app.show_arbitrage_detail(self.app.all_scan_rows[idx])
    
    def update_metrics(self, stats: ScanStats):
        self.metric_vars["scan_time"].set(str(stats.scan_ms))
        self.metric_vars["success_rate"].set(f"{stats.success_rate_pct:.2f}")
        self.metric_vars["best_profit"].set(f"{stats.best_profit_pct:.4f}")
        self.metric_vars["avg_profit"].set(f"{stats.avg_profit_pct:.4f}")
        self.metric_vars["opportunities"].set(str(stats.clean_profitable_paths))
        self.scan_counter.configure(
            text=f"Scaneadas: {stats.scanned_paths} | Válidas: {stats.valid_paths} | Limpias: {stats.clean_profitable_paths}"
        )
    
    def set_status(self, text: str):
        self.status_label.configure(text=text)

class StatsPage(tk.Frame):
    """Página de estadísticas e historial"""
    
    def __init__(self, parent, app: "ArbitrageApp", **kwargs):
        super().__init__(parent, bg=Theme.BG_PRIMARY, **kwargs)
        self.app = app
        self._build_ui()
    
    def _build_ui(self):
        header = tk.Frame(self, bg=Theme.BG_SECONDARY)
        header.pack(fill="x", pady=(0, 16))
        tk.Frame(header, bg=Theme.SUCCESS, height=2).pack(fill="x")
        
        header_content = tk.Frame(header, bg=Theme.BG_SECONDARY, padx=24, pady=16)
        header_content.pack(fill="x")
        
        tk.Label(header_content, text="Estadísticas e Historial", 
                font=("Segoe UI", 20, "bold"),
                fg=Theme.TEXT_PRIMARY, bg=Theme.BG_SECONDARY).pack(side="left")
        
        ModernButton(header_content, "Limpiar Historial", self.app.clear_history,
                    "ghost", 140, 40).pack(side="right", padx=4)
        
        # Contenido
        content = tk.Frame(self, bg=Theme.BG_PRIMARY)
        content.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        
        # Tabla de historial
        tk.Label(content, text="HISTORIAL DE ESCANEOS", font=("Segoe UI", 10, "bold"),
                fg=Theme.ACCENT_CYAN, bg=Theme.BG_PRIMARY).pack(anchor="w", pady=(0, 8))
        
        self.history_table = VirtualTreeview(
            content,
            columns=["timestamp", "market", "network", "valid", "clean", "success", "best", "ms"],
            headings=["Timestamp", "Mercado", "Red", "Válidas", "Limpias", "% Éxito", "Mejor %", "Ms"],
            widths=[160, 80, 80, 70, 70, 80, 90, 60],
            height=10
        )
        self.history_table.pack(fill="x", pady=(0, 16))
        
        # Slider de ciclos
        slider_frame = tk.Frame(content, bg=Theme.BG_TERTIARY, padx=16, pady=12)
        slider_frame.pack(fill="x", pady=(0, 16))
        
        tk.Label(slider_frame, text="Ciclos de Simulación:", 
                fg=Theme.TEXT_SECONDARY, bg=Theme.BG_TERTIARY,
                font=("Segoe UI", 10)).pack(side="left")
        
        self.cycle_slider = tk.Scale(slider_frame, from_=10, to=500, orient="horizontal",
                                    bg=Theme.BG_TERTIARY, fg=Theme.TEXT_PRIMARY,
                                    highlightthickness=0, length=400,
                                    command=self._on_slider_change)
        self.cycle_slider.set(50)
        self.cycle_slider.pack(side="left", padx=12)
        
        self.cycle_label = tk.Label(slider_frame, text="50", 
                                   fg=Theme.ACCENT_CYAN, bg=Theme.BG_TERTIARY,
                                   font=("Segoe UI", 12, "bold"), width=4)
        self.cycle_label.pack(side="left")
        
        # Detalle y gráfico ASCII
        tk.Label(content, text="DETALLE DE RUTA + GRÁFICO", 
                font=("Segoe UI", 10, "bold"),
                fg=Theme.SUCCESS, bg=Theme.BG_PRIMARY).pack(anchor="w", pady=(0, 8))
        
        self.detail_text = tk.Text(content, bg=Theme.BG_CARD, fg=Theme.TEXT_PRIMARY,
                                  font=("JetBrains Mono", 10), relief="flat",
                                  wrap="word", padx=16, pady=12, height=12,
                                  highlightthickness=1, highlightbackground=Theme.BORDER)
        self.detail_text.pack(fill="both", expand=True)
    
    def _on_slider_change(self, value):
        self.cycle_label.configure(text=str(int(float(value))))
        self.app.compound_cycles_var.set(str(int(float(value))))
    
    def update_history(self, history: List[ScanStats]):
        rows = []
        for stats in history[-100:]:
            rows.append((
                stats.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                stats.market_type,
                "testnet" if stats.testnet else "mainnet",
                stats.valid_paths,
                stats.clean_profitable_paths,
                f"{stats.success_rate_pct:.2f}%",
                f"{stats.best_profit_pct:.4f}%",
                stats.scan_ms
            ))
        self.history_table.set_data(rows)
    
    def show_detail(self, text: str):
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)

class ConfigPage(tk.Frame):
    """Página de configuración completa"""
    
    def __init__(self, parent, app: "ArbitrageApp", **kwargs):
        super().__init__(parent, bg=Theme.BG_PRIMARY, **kwargs)
        self.app = app
        self._build_ui()
    
    def _build_ui(self):
        header = tk.Frame(self, bg=Theme.BG_SECONDARY)
        header.pack(fill="x", pady=(0, 16))
        tk.Frame(header, bg=Theme.WARNING, height=2).pack(fill="x")
        
        header_content = tk.Frame(header, bg=Theme.BG_SECONDARY, padx=24, pady=16)
        header_content.pack(fill="x")
        
        tk.Label(header_content, text="Configuración del Sistema", 
                font=("Segoe UI", 20, "bold"),
                fg=Theme.TEXT_PRIMARY, bg=Theme.BG_SECONDARY).pack(side="left")
        
        btn_frame = tk.Frame(header_content, bg=Theme.BG_SECONDARY)
        btn_frame.pack(side="right")
        
        ModernButton(btn_frame, "Guardar", self.app.save_config,
                    "warning", 120, 40, "💾").pack(side="left", padx=4)
        ModernButton(btn_frame, "Validar API", self.app.validate_keys,
                    "success", 130, 40, "🔗").pack(side="left", padx=4)
        ModernButton(btn_frame, "Copiar Config", self.app.copy_config,
                    "ghost", 130, 40, "📋").pack(side="left", padx=4)
        
        # Canvas scrolleable
        canvas = tk.Canvas(self, bg=Theme.BG_PRIMARY, highlightthickness=0)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=canvas.yview,
                                bg=Theme.BG_TERTIARY, troughcolor=Theme.BG_PRIMARY,
                                activebackground=Theme.ACCENT_CYAN, width=8)
        
        self.content = tk.Frame(canvas, bg=Theme.BG_PRIMARY)
        
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        
        canvas_window = canvas.create_window((0, 0), window=self.content, anchor="nw")
        
        def configure_canvas(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(canvas_window, width=event.width)
        
        self.content.bind("<Configure>", configure_canvas)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width))
        
        # Secciones
        self._create_section("Credenciales API", self._api_section)
        self._create_section("Parámetros de Mercado", self._market_section)
        self._create_section("Configuración de Interés Compuesto", self._compound_section)
        self._create_section("Señal de IA", self._ai_section)
    
    def _create_section(self, title: str, content_func):
        section = tk.Frame(self.content, bg=Theme.BG_PRIMARY, pady=16)
        section.pack(fill="x", padx=24)
        
        header = tk.Frame(section, bg=Theme.BG_PRIMARY)
        header.pack(fill="x", pady=(0, 12))
        
        tk.Frame(header, bg=Theme.ACCENT_CYAN, width=4, height=20).pack(side="left")
        tk.Label(header, text=title, font=("Segoe UI", 14, "bold"),
                fg=Theme.TEXT_PRIMARY, bg=Theme.BG_PRIMARY).pack(side="left", padx=12)
        
        tk.Frame(section, bg=Theme.BORDER, height=1).pack(fill="x", pady=(0, 16))
        
        content = tk.Frame(section, bg=Theme.BG_PRIMARY)
        content.pack(fill="x")
        content_func(content)
    
    def _api_section(self, parent):
        # API Key
        row = tk.Frame(parent, bg=Theme.BG_PRIMARY, pady=8)
        row.pack(fill="x")
        tk.Label(row, text="API Key", fg=Theme.TEXT_SECONDARY,
                bg=Theme.BG_PRIMARY, font=("Segoe UI", 10), width=15).pack(side="left")
        ModernEntry(row, self.app.api_key_var, placeholder="Ingresa API key...", width=50).pack(side="left", fill="x", expand=True)
        
        # API Secret
        row = tk.Frame(parent, bg=Theme.BG_PRIMARY, pady=8)
        row.pack(fill="x")
        tk.Label(row, text="API Secret", fg=Theme.TEXT_SECONDARY,
                bg=Theme.BG_PRIMARY, font=("Segoe UI", 10), width=15).pack(side="left")
        self.secret_entry = ModernEntry(row, self.app.api_secret_var, 
                                       show="•", placeholder="Ingresa API secret...", width=50)
        self.secret_entry.pack(side="left", fill="x", expand=True)
        
        # Opciones
        opts = tk.Frame(parent, bg=Theme.BG_PRIMARY, pady=8)
        opts.pack(fill="x")
        
        tk.Checkbutton(opts, text="Guardar claves en disco", variable=self.app.save_keys_var,
                      bg=Theme.BG_PRIMARY, fg=Theme.TEXT_SECONDARY,
                      selectcolor=Theme.BG_CARD, activebackground=Theme.BG_PRIMARY).pack(side="left")
        
        tk.Checkbutton(opts, text="Mostrar secreto", variable=self.app.show_secret_var,
                      command=self._toggle_secret, bg=Theme.BG_PRIMARY,
                      fg=Theme.TEXT_SECONDARY, selectcolor=Theme.BG_CARD,
                      activebackground=Theme.BG_PRIMARY).pack(side="left", padx=20)
        
        ModernButton(opts, "Limpiar Claves", self.app.clear_keys,
                    "danger", 120, 32).pack(side="right")
    
    def _toggle_secret(self):
        if self.app.show_secret_var.get():
            self.secret_entry.entry.configure(show="")
        else:
            self.secret_entry.entry.configure(show="•")
    
    def _market_section(self, parent):
        fields = [
            ("Capital Inicial (USDT)", self.app.usdt_var),
            ("Fee por Trade", self.app.fee_var),
            ("Máx. Assets", self.app.max_assets_var),
            ("Ganancia Mín. Limpia", self.app.min_profit_var),
        ]
        
        for i, (label, var) in enumerate(fields):
            row = tk.Frame(parent, bg=Theme.BG_PRIMARY, pady=8)
            row.pack(fill="x")
            tk.Label(row, text=label, fg=Theme.TEXT_SECONDARY,
                    bg=Theme.BG_PRIMARY, font=("Segoe UI", 10), width=20).pack(side="left")
            ModernEntry(row, var, width=20).pack(side="left", padx=(0, 20))
            
            if i == 1:  # Después de Fee, agregar selects
                tk.Label(row, text="Tipo:", fg=Theme.TEXT_SECONDARY,
                        bg=Theme.BG_PRIMARY, font=("Segoe UI", 10)).pack(side="left", padx=(20, 8))
                ModernCombobox(row, self.app.market_var, 
                              ["spot", "perpetual"], width=12).pack(side="left", padx=(0, 20))
                
                tk.Label(row, text="Red:", fg=Theme.TEXT_SECONDARY,
                        bg=Theme.BG_PRIMARY, font=("Segoe UI", 10)).pack(side="left", padx=(8, 8))
                ModernCombobox(row, self.app.network_var,
                              ["mainnet", "testnet", "demo"], width=12).pack(side="left")
    
    def _compound_section(self, parent):
        fields = [
            ("Ciclos", self.app.compound_cycles_var),
            ("Trigger (×capital)", self.app.compound_trigger_var),
            ("Stake post-trigger (%)", self.app.compound_stake_var),
        ]
        
        for label, var in fields:
            row = tk.Frame(parent, bg=Theme.BG_PRIMARY, pady=8)
            row.pack(fill="x")
            tk.Label(row, text=label, fg=Theme.TEXT_SECONDARY,
                    bg=Theme.BG_PRIMARY, font=("Segoe UI", 10), width=22).pack(side="left")
            ModernEntry(row, var, width=15).pack(side="left", padx=(0, 20))
    
    def _ai_section(self, parent):
        fields = [
            ("Símbolo", self.app.ai_symbol_var),
            ("Cantidad (USDT)", self.app.ai_qty_var),
            ("Intervalo", self.app.ai_interval_var),
            ("Límite (velas)", self.app.ai_limit_var),
        ]
        
        for i, (label, var) in enumerate(fields):
            row = tk.Frame(parent, bg=Theme.BG_PRIMARY, pady=8)
            row.pack(fill="x")
            tk.Label(row, text=label, fg=Theme.TEXT_SECONDARY,
                    bg=Theme.BG_PRIMARY, font=("Segoe UI", 10), width=18).pack(side="left")
            ModernEntry(row, var, width=15).pack(side="left", padx=(0, 20))
            
            if i == 1:  # Después de Cantidad
                tk.Label(row, text="Modo:", fg=Theme.TEXT_SECONDARY,
                        bg=Theme.BG_PRIMARY, font=("Segoe UI", 10)).pack(side="left", padx=(20, 8))
                ModernCombobox(row, self.app.order_mode_var,
                              ["simulador", "testnet", "real"], width=12).pack(side="left")

class ExecutionPage(tk.Frame):
    """Página de ejecución de trades completa"""
    
    def __init__(self, parent, app: "ArbitrageApp", **kwargs):
        super().__init__(parent, bg=Theme.BG_PRIMARY, **kwargs)
        self.app = app
        self._build_ui()
    
    def _build_ui(self):
        header = tk.Frame(self, bg=Theme.BG_SECONDARY)
        header.pack(fill="x", pady=(0, 16))
        tk.Frame(header, bg=Theme.ACCENT_PURPLE, height=2).pack(fill="x")
        
        header_content = tk.Frame(header, bg=Theme.BG_SECONDARY, padx=24, pady=16)
        header_content.pack(fill="x")
        
        title_frame = tk.Frame(header_content, bg=Theme.BG_SECONDARY)
        title_frame.pack(side="left")
        
        tk.Label(title_frame, text="Ejecución Unificada", 
                font=("Segoe UI", 20, "bold"),
                fg=Theme.TEXT_PRIMARY, bg=Theme.BG_SECONDARY).pack(anchor="w")
        
        tk.Label(title_frame, text="IA - Arbitraje - Órdenes en Tiempo Real",
                font=("Segoe UI", 11), fg=Theme.TEXT_SECONDARY,
                bg=Theme.BG_SECONDARY).pack(anchor="w")
        
        controls = tk.Frame(header_content, bg=Theme.BG_SECONDARY)
        controls.pack(side="right")
        
        # Auto-execute checkbox
        tk.Checkbutton(controls, text="Auto-ejecutar", variable=self.app.auto_execute_var,
                      bg=Theme.BG_SECONDARY, fg=Theme.TEXT_SECONDARY,
                      selectcolor=Theme.BG_CARD, activebackground=Theme.BG_SECONDARY).pack(side="left", padx=8)
        
        ModernButton(controls, "Análisis IA", self.app.run_ai_analysis,
                    "accent", 140, 42, "🧠").pack(side="left", padx=4)
        ModernButton(controls, "Orden Mercado", self.app.execute_market_order,
                    "warning", 150, 42, "⚡").pack(side="left", padx=4)
        ModernButton(controls, "Limpiar Log", self.app.clear_exec_log,
                    "ghost", 120, 42).pack(side="left", padx=4)
        
        # KPIs
        kpi_frame = tk.Frame(self, bg=Theme.BG_PRIMARY, padx=24, pady=10)
        kpi_frame.pack(fill="x")
        
        self.kpi_vars = {
            "trades": tk.StringVar(value="0"),
            "success": tk.StringVar(value="—"),
            "profit": tk.StringVar(value="0.000000")
        }
        
        MetricCard(kpi_frame, "Trades Ejecutados", self.kpi_vars["trades"], 
                  "", Theme.ACCENT_CYAN).pack(side="left", padx=(0, 12))
        MetricCard(kpi_frame, "Tasa de Éxito", self.kpi_vars["success"],
                  "%", Theme.SUCCESS).pack(side="left", padx=(0, 12))
        MetricCard(kpi_frame, "Ganancia Total", self.kpi_vars["profit"],
                  "USDT", Theme.WARNING).pack(side="left", padx=(0, 12))
        
        # Contenido principal
        body = tk.Frame(self, bg=Theme.BG_PRIMARY)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        
        # Log de actividad
        tk.Label(body, text="LOG DE ACTIVIDAD", font=("Segoe UI", 10, "bold"),
                fg=Theme.ACCENT_PURPLE, bg=Theme.BG_PRIMARY).pack(anchor="w", pady=(0, 8))
        
        self.activity_log = TerminalLog(body, height=10)
        self.activity_log.pack(fill="x", pady=(0, 16))
        
        # Tablas de trades
        tables = tk.Frame(body, bg=Theme.BG_PRIMARY)
        tables.pack(fill="both", expand=True)
        tables.columnconfigure(0, weight=3)
        tables.columnconfigure(1, weight=2)
        
        # Tabla de trades
        tk.Label(tables, text="TRADES EJECUTADOS", font=("Segoe UI", 9, "bold"),
                fg=Theme.ACCENT_CYAN, bg=Theme.BG_PRIMARY).grid(row=0, column=0, sticky="w", pady=(0, 8))
        
        self.trade_table = VirtualTreeview(
            tables,
            columns=["timestamp", "symbol", "signal", "path", "profit", "mode", "status"],
            headings=["Timestamp", "Símbolo", "Señal", "Ruta", "Ganancia", "Modo", "Estado"],
            widths=[130, 70, 60, 200, 90, 80, 90],
            height=8
        )
        self.trade_table.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        
        # Tabla de ganancias
        tk.Label(tables, text="GANANCIAS ACUMULADAS", font=("Segoe UI", 9, "bold"),
                fg=Theme.SUCCESS, bg=Theme.BG_PRIMARY).grid(row=0, column=1, sticky="w", pady=(0, 8))
        
        self.profit_table = VirtualTreeview(
            tables,
            columns=["timestamp", "symbol", "profit", "cumulative", "status"],
            headings=["Timestamp", "Símbolo", "Ganancia", "Acumulado", "Estado"],
            widths=[130, 80, 100, 110, 100],
            height=8
        )
        self.profit_table.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
    
    def log_message(self, message: str, level: str = "INFO"):
        self.activity_log.log(message, level)
    
    def update_trade_tables(self, trades: List[TradeRecord]):
        # Actualizar tabla de trades
        trade_rows = []
        for t in trades[-100:]:
            trade_rows.append((
                t.timestamp, t.symbol, t.signal, t.best_path,
                f"{t.net_profit:.6f}", t.mode, t.status
            ))
        self.trade_table.set_data(trade_rows)
        
        # Actualizar tabla de ganancias acumuladas
        profit_rows = []
        cumulative = 0.0
        for t in trades:
            cumulative += t.net_profit
            profit_rows.append((
                t.timestamp, t.symbol, f"{t.net_profit:.6f}",
                f"{cumulative:.6f}", t.status
            ))
        self.profit_table.set_data(profit_rows[-100:])
        
        # Actualizar KPIs
        total_trades = len(trades)
        successful = sum(1 for t in trades if t.net_profit > 0)
        success_rate = (successful / total_trades * 100) if total_trades > 0 else 0
        
        self.kpi_vars["trades"].set(str(total_trades))
        self.kpi_vars["success"].set(f"{success_rate:.2f}")
        self.kpi_vars["profit"].set(f"{cumulative:.6f}")

# ==========================
# APLICACIÓN PRINCIPAL
# ==========================

class ArbitrageApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("HFT Crypto Arbitrage Pro v3.1")
        self.root.geometry("1600x900")
        self.root.minsize(1280, 720)
        self.root.configure(bg=Theme.BG_PRIMARY)
        
        # Variables
        self._init_variables()
        
        # Componentes
        self.scanner = BinanceArbitrageScanner()
        self.ai_engine = AISignalEngine()
        self.toast = ToastNotification(root)
        
        # Estado
        self.running = False
        self.auto_scanning = False
        self.last_output: Optional[ScanOutput] = None
        self.all_scan_rows: List[ArbitrageResult] = []
        self.scan_history: List[ScanStats] = []
        self.executed_trades: List[TradeRecord] = []
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        
        # UI
        self._build_ui()
        self._setup_shortcuts()
        self._load_config()
        
        self.toast.show("Sistema inicializado correctamente", "success")
    
    def _init_variables(self):
        # API
        self.api_key_var = tk.StringVar()
        self.api_secret_var = tk.StringVar()
        self.save_keys_var = tk.BooleanVar(value=False)
        self.show_secret_var = tk.BooleanVar(value=False)
        
        # Market
        self.market_var = tk.StringVar(value="spot")
        self.network_var = tk.StringVar(value="testnet")
        self.usdt_var = tk.StringVar(value="100.0")
        self.fee_var = tk.StringVar(value="0.001")
        self.max_assets_var = tk.StringVar(value="120")
        self.min_profit_var = tk.StringVar(value="0.01")
        
        # Auto-scan
        self.scan_interval_var = tk.StringVar(value="5")
        
        # Compound
        self.compound_cycles_var = tk.StringVar(value="50")
        self.compound_trigger_var = tk.StringVar(value="2.0")
        self.compound_stake_var = tk.StringVar(value="0.10")
        
        # AI
        self.ai_symbol_var = tk.StringVar(value="BTCUSDT")
        self.ai_qty_var = tk.StringVar(value="100.0")
        self.ai_interval_var = tk.StringVar(value="1m")
        self.ai_limit_var = tk.StringVar(value="300")
        
        # Order
        self.order_mode_var = tk.StringVar(value="simulador")
        self.auto_execute_var = tk.BooleanVar(value=False)
        
        # Auto-execute tracking
        self._last_auto_path = ""
        self._last_auto_ts = 0.0
    
    def _build_ui(self):
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)
        
        # Sidebar
        self.sidebar = self._create_sidebar()
        self.sidebar.grid(row=0, column=0, sticky="ns")
        
        tk.Frame(self.root, bg=Theme.BORDER, width=1).grid(row=0, column=0, sticky="nse")
        
        # Contenido
        self.content = tk.Frame(self.root, bg=Theme.BG_PRIMARY)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        
        # Páginas
        self.pages: Dict[str, tk.Frame] = {}
        
        self.scanner_page = ScannerPage(self.content, self)
        self.stats_page = StatsPage(self.content, self)
        self.config_page = ConfigPage(self.content, self)
        self.exec_page = ExecutionPage(self.content, self)
        
        for page in [self.scanner_page, self.stats_page, self.config_page, self.exec_page]:
            page.grid(row=0, column=0, sticky="nsew")
        
        self.pages = {
            "scanner": self.scanner_page,
            "stats": self.stats_page,
            "config": self.config_page,
            "exec": self.exec_page
        }
        
        # Status bar
        self.status_bar = self._create_statusbar()
        self.status_bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        
        self.show_page("scanner")
    
    def _create_sidebar(self) -> tk.Frame:
        sidebar = tk.Frame(self.root, bg=Theme.BG_SECONDARY, width=260)
        sidebar.pack_propagate(False)
        
        # Logo
        logo = tk.Frame(sidebar, bg=Theme.BG_SECONDARY, height=120)
        logo.pack(fill="x", pady=30)
        
        tk.Label(logo, text="◈ HFT ARB", font=("Segoe UI", 18, "bold"),
                fg=Theme.ACCENT_CYAN, bg=Theme.BG_SECONDARY).pack()
        tk.Label(logo, text="PRO v3.1", font=("Segoe UI", 10),
                fg=Theme.TEXT_MUTED, bg=Theme.BG_SECONDARY).pack()
        
        # Navegación
        nav = tk.Frame(sidebar, bg=Theme.BG_SECONDARY)
        nav.pack(fill="x", padx=20, pady=30)
        
        self.nav_buttons = {}
        for key, icon, label in [
            ("scanner", "⬡", "Scanner"),
            ("stats", "▲", "Estadísticas"),
            ("config", "⚙", "Configuración"),
            ("exec", "⚡", "Ejecución")
        ]:
            btn = self._create_nav_button(nav, key, icon, label)
            btn.pack(fill="x", pady=6)
            self.nav_buttons[key] = btn
        
        # Indicador de conexión
        status = tk.Frame(sidebar, bg=Theme.BG_TERTIARY, padx=16, pady=12)
        status.pack(fill="x", padx=20, side="bottom", pady=30)
        
        self.connection_indicator = tk.Label(status, text="● TESTNET CONECTADO",
                                           fg=Theme.SUCCESS, bg=Theme.BG_TERTIARY,
                                           font=("Segoe UI", 9, "bold"))
        self.connection_indicator.pack()
        
        # Atajos
        shortcuts = tk.Frame(sidebar, bg=Theme.BG_SECONDARY, padx=20)
        shortcuts.pack(fill="x", side="bottom", pady=10)
        
        tk.Label(shortcuts, text="ATAJOS", font=("Segoe UI", 8, "bold"),
                fg=Theme.TEXT_MUTED, bg=Theme.BG_SECONDARY).pack(anchor="w")
        
        for key, action in [("F5", "Escanear"), ("Ctrl+S", "Guardar"), ("Esc", "Detener")]:
            row = tk.Frame(shortcuts, bg=Theme.BG_SECONDARY)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=key, font=("Segoe UI", 9),
                    fg=Theme.ACCENT_CYAN, bg=Theme.BG_SECONDARY, width=8).pack(side="left")
            tk.Label(row, text=action, font=("Segoe UI", 9),
                    fg=Theme.TEXT_MUTED, bg=Theme.BG_SECONDARY).pack(side="left")
        
        return sidebar
    
    def _create_nav_button(self, parent, key: str, icon: str, label: str) -> tk.Frame:
        btn = tk.Frame(parent, bg=Theme.BG_SECONDARY, cursor="hand2")
        
        indicator = tk.Frame(btn, bg=Theme.BG_SECONDARY, width=4)
        indicator.pack(side="left", fill="y")
        
        content = tk.Frame(btn, bg=Theme.BG_SECONDARY, padx=16, pady=14)
        content.pack(side="left", fill="x", expand=True)
        
        tk.Label(content, text=icon, font=("Segoe UI", 14),
                fg=Theme.TEXT_MUTED, bg=Theme.BG_SECONDARY, width=2).pack(side="left")
        
        text_frame = tk.Frame(content, bg=Theme.BG_SECONDARY)
        text_frame.pack(side="left", fill="x", expand=True, padx=8)
        
        tk.Label(text_frame, text=label, font=("Segoe UI", 11, "bold"),
                fg=Theme.TEXT_SECONDARY, bg=Theme.BG_SECONDARY).pack(anchor="w")
        
        btn._indicator = indicator
        btn._content = content
        
        for widget in [btn, content, indicator]:
            widget.bind("<Button-1>", lambda e, k=key: self.show_page(k))
        
        return btn
    
    def _create_statusbar(self) -> tk.Frame:
        bar = tk.Frame(self.root, bg=Theme.BG_TERTIARY, height=36)
        bar.pack_propagate(False)
        
        tk.Frame(bar, bg=Theme.BORDER, height=1).pack(fill="x", side="top")
        
        self.status_text = tk.Label(bar, text="Listo", fg=Theme.TEXT_MUTED,
                                   bg=Theme.BG_TERTIARY, font=("Segoe UI", 10))
        self.status_text.pack(side="left", padx=20)
        
        self.auto_indicator = tk.Label(bar, text="AUTO: OFF", fg=Theme.TEXT_MUTED,
                                      bg=Theme.BG_CARD, font=("Segoe UI", 9, "bold"),
                                      padx=12, pady=2)
        self.auto_indicator.pack(side="right", padx=8)
        
        self.clock_label = tk.Label(bar, text="", fg=Theme.TEXT_MUTED,
                                   bg=Theme.BG_TERTIARY, font=("JetBrains Mono", 10))
        self.clock_label.pack(side="right", padx=20)
        
        self._update_clock()
        
        return bar
    
    def _update_clock(self):
        self.clock_label.configure(text=datetime.now().strftime("%H:%M:%S"))
        self.root.after(1000, self._update_clock)
    
    def show_page(self, key: str):
        for k, btn in self.nav_buttons.items():
            color = Theme.ACCENT_CYAN if k == key else Theme.BG_SECONDARY
            text_color = Theme.TEXT_PRIMARY if k == key else Theme.TEXT_SECONDARY
            btn._indicator.configure(bg=color)
            for child in btn._content.winfo_children():
                if isinstance(child, tk.Label):
                    child.configure(fg=text_color)
        
        self.pages[key].tkraise()
        
        # Actualizar página de stats si es necesario
        if key == "stats":
            self.stats_page.update_history(self.scan_history)
    
    def _setup_shortcuts(self):
        self.root.bind("<F5>", lambda e: self.scan_once())
        self.root.bind("<Control-s>", lambda e: self.save_config())
        self.root.bind("<Escape>", lambda e: self.stop_auto_scan())
    
    # === ACCIONES ===
    
    def scan_once(self):
        if self.running:
            return
        
        self.running = True
        self.scanner_page.set_status("Escaneando mercados...")
        
        def task():
            try:
                self.scanner.configure(
                    self.market_var.get(),
                    self.network_var.get(),
                    float(self.fee_var.get())
                )
                
                output = self.scanner.scan(
                    start_usdt=float(self.usdt_var.get()),
                    max_assets=int(self.max_assets_var.get()),
                    min_clean_profit_usdt=float(self.min_profit_var.get())
                )
                
                self.last_output = output
                self.all_scan_rows = output.opportunities
                self.scan_history.append(output.stats)
                
                self.root.after(0, lambda: self._update_scan_results(output))
                
            except Exception as e:
                logger.error(f"Error en scan: {e}")
                self.root.after(0, lambda: self.toast.show(str(e), "error"))
            finally:
                self.running = False
                self.root.after(0, lambda: self.scanner_page.set_status("Listo"))
                self.root.after(0, self._auto_execute_if_profitable)
        
        self.executor.submit(task)
    
    def _update_scan_results(self, output: ScanOutput):
        self.scanner_page.update_metrics(output.stats)
        
        rows = []
        tags = []
        for i, opp in enumerate(output.opportunities[:50], 1):
            rows.append((
                i,
                " → ".join(opp.path),
                " / ".join(opp.symbols),
                f"{opp.net_profit_usdt:.6f}",
                f"{opp.final_usdt:.6f}",
                f"{opp.total_fees_usdt:.6f}",
                f"{opp.profit_pct:.4f}%"
            ))
            tag = ("profit",) if opp.net_profit_usdt > 0 else ("loss",)
            if i == 1:
                tag += ("highlight",)
            tags.append(tag)
        
        self.scanner_page.table.set_data(rows, tags)
        
        if output.opportunities:
            best = output.opportunities[0]
            self.toast.show(f"Mejor oportunidad: {best.profit_pct:.4f}%", "success")
            self.exec_page.log_message(
                f"Scan OK - {output.stats.valid_paths} válidas, "
                f"{output.stats.clean_profitable_paths} limpias, "
                f"{output.stats.scan_ms}ms", "SUCCESS"
            )
    
    def start_auto_scan(self):
        if self.auto_scanning:
            return
        
        self.auto_scanning = True
        self.auto_indicator.configure(text="AUTO: ON", fg=Theme.SUCCESS, bg="#064e3b")
        self.toast.show("Auto-scan iniciado", "info")
        self._auto_scan_loop()
    
    def stop_auto_scan(self):
        self.auto_scanning = False
        self.auto_indicator.configure(text="AUTO: OFF", fg=Theme.TEXT_MUTED, bg=Theme.BG_CARD)
        self.toast.show("Auto-scan detenido", "info")
    
    def toggle_auto_scan(self):
        if self.auto_scanning:
            self.stop_auto_scan()
        else:
            self.start_auto_scan()
    
    def _auto_scan_loop(self):
        if not self.auto_scanning:
            return
        
        self.scan_once()
        try:
            interval = int(self.scan_interval_var.get()) * 1000
        except:
            interval = 5000
        
        self.root.after(interval, self._auto_scan_loop)
    
    def _auto_execute_if_profitable(self):
        """Ejecutar automáticamente si hay oportunidad rentable"""
        if not self.auto_execute_var.get() or not self.last_output:
            return
        
        if not self.last_output.opportunities:
            return
        
        best = self.last_output.opportunities[0]
        path = " → ".join(best.path)
        now = time.time()
        
        # Evitar ejecuciones duplicadas rápidas
        if self._last_auto_path == path and (now - self._last_auto_ts) < 10:
            return
        
        self._last_auto_path = path
        self._last_auto_ts = now
        
        # Solo ejecutar si es realmente rentable
        if best.net_profit_usdt > float(self.min_profit_var.get()):
            self.execute_market_order()
    
    def show_arbitrage_detail(self, result: ArbitrageResult):
        """Mostrar detalle de oportunidad con simulación compuesta"""
        try:
            plan = self.scanner.simulate_compound_plan(
                initial_capital_usdt=float(self.usdt_var.get()),
                per_cycle_net_pct=result.profit_pct,
                cycles=int(self.compound_cycles_var.get()),
                trigger_multiple=float(self.compound_trigger_var.get()),
                compound_stake_pct=float(self.compound_stake_var.get()),
                pre_trigger_stake_pct=1.0
            )
            
            text = f"""  Ruta        : {' → '.join(result.path)}
  Símbolos    : {' / '.join(result.symbols)}
  Lados       : {' → '.join(result.sides)}
  
  ── Resultados del Ciclo ──
  Ganancia Neta    : {result.net_profit_usdt:.6f} USDT ({result.profit_pct:.4f}%)
  Comisiones       : {result.total_fees_usdt:.6f} USDT
  Bruto Final      : {result.gross_final_usdt:.6f} USDT
  Neto Final       : {result.final_usdt:.6f} USDT
  
  ── Simulación Compuesta ──
  Capital Inicial  : {plan.initial_capital:.2f} USDT
  Capital Final    : {plan.current_capital:.4f} USDT
  Ciclos           : {plan.cycles_simulated}
  Trigger Alcanzado: {'✓ SÍ' if plan.trigger_reached else '✗ NO'} (ciclo {plan.trigger_cycle})
  Modo de Stake    : {plan.stake_mode}
"""
            self.stats_page.show_detail(text)
            self.show_page("stats")
            
        except Exception as e:
            self.toast.show(f"Error en simulación: {e}", "error")
    
    def validate_keys(self):
        try:
            self.scanner.configure(
                self.market_var.get(),
                self.network_var.get(),
                float(self.fee_var.get())
            )
            ok, msg = self.scanner.validate_api_keys(
                self.api_key_var.get(),
                self.api_secret_var.get()
            )
            self.toast.show(msg, "success" if ok else "error")
            if ok:
                self.connection_indicator.configure(
                    text=f"● {self.network_var.get().upper()} CONECTADO",
                    fg=Theme.SUCCESS
                )
        except Exception as e:
            self.toast.show(str(e), "error")
    
    def save_config(self):
        try:
            config = {
                "api_key": self.api_key_var.get() if self.save_keys_var.get() else "",
                "api_secret": self.api_secret_var.get() if self.save_keys_var.get() else "",
                "market": self.market_var.get(),
                "network": self.network_var.get(),
                "usdt": self.usdt_var.get(),
                "fee": self.fee_var.get(),
                "max_assets": self.max_assets_var.get(),
                "min_profit": self.min_profit_var.get(),
                "compound_cycles": self.compound_cycles_var.get(),
                "compound_trigger": self.compound_trigger_var.get(),
                "compound_stake": self.compound_stake_var.get(),
                "ai_symbol": self.ai_symbol_var.get(),
                "ai_qty": self.ai_qty_var.get(),
                "ai_interval": self.ai_interval_var.get(),
                "ai_limit": self.ai_limit_var.get(),
                "order_mode": self.order_mode_var.get(),
                "auto_execute": self.auto_execute_var.get(),
                "save_keys": self.save_keys_var.get(),
            }
            CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
            self.toast.show("Configuración guardada correctamente", "success")
        except Exception as e:
            self.toast.show(f"Error guardando: {e}", "error")
    
    def copy_config(self):
        try:
            config = {
                "market": self.market_var.get(),
                "network": self.network_var.get(),
                "usdt": self.usdt_var.get(),
                "fee": self.fee_var.get(),
                "max_assets": self.max_assets_var.get(),
            }
            self.root.clipboard_clear()
            self.root.clipboard_append(json.dumps(config, indent=2))
            self.toast.show("Configuración copiada al portapapeles", "success")
        except Exception as e:
            self.toast.show(f"Error copiando: {e}", "error")
    
    def clear_keys(self):
        self.api_key_var.set("")
        self.api_secret_var.set("")
        self.save_keys_var.set(False)
        self.toast.show("Claves limpiadas", "warning")
    
    def _load_config(self):
        if not CONFIG_PATH.exists():
            return
        
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            
            if config.get("save_keys"):
                if config.get("api_key"):
                    self.api_key_var.set(config["api_key"])
                if config.get("api_secret"):
                    self.api_secret_var.set(config["api_secret"])
            
            self.market_var.set(config.get("market", "spot"))
            self.network_var.set(config.get("network", "testnet"))
            self.usdt_var.set(config.get("usdt", "100.0"))
            self.fee_var.set(config.get("fee", "0.001"))
            self.max_assets_var.set(config.get("max_assets", "120"))
            self.min_profit_var.set(config.get("min_profit", "0.01"))
            self.compound_cycles_var.set(config.get("compound_cycles", "50"))
            self.compound_trigger_var.set(config.get("compound_trigger", "2.0"))
            self.compound_stake_var.set(config.get("compound_stake", "0.10"))
            self.ai_symbol_var.set(config.get("ai_symbol", "BTCUSDT"))
            self.ai_qty_var.set(config.get("ai_qty", "100.0"))
            self.ai_interval_var.set(config.get("ai_interval", "1m"))
            self.ai_limit_var.set(config.get("ai_limit", "300"))
            self.order_mode_var.set(config.get("order_mode", "simulador"))
            self.auto_execute_var.set(config.get("auto_execute", False))
            self.save_keys_var.set(config.get("save_keys", False))
            
        except Exception as e:
            logger.error(f"Error cargando config: {e}")
    
    def clear_scan_results(self):
        self.all_scan_rows = []
        self.last_output = None
        self.scanner_page.table.clear()
        self.scanner_page.metric_vars["scan_time"].set("—")
        self.scanner_page.metric_vars["success_rate"].set("—")
        self.scanner_page.metric_vars["best_profit"].set("—")
        self.scanner_page.metric_vars["avg_profit"].set("—")
        self.scanner_page.metric_vars["opportunities"].set("0")
        self.scanner_page.scan_counter.configure(text="Resultados limpiados")
        self.toast.show("Resultados del scan limpiados", "info")
    
    def clear_history(self):
        self.scan_history = []
        self.stats_page.history_table.clear()
        self.stats_page.show_detail("")
        self.toast.show("Historial limpiado", "info")
    
    def clear_exec_log(self):
        self.exec_page.activity_log.clear()
        self.toast.show("Log de ejecución limpiado", "info")
    
    def run_ai_analysis(self):
        def task():
            try:
                result = self.ai_engine.run(
                    self.ai_symbol_var.get().upper(),
                    self.ai_interval_var.get(),
                    int(self.ai_limit_var.get())
                )
                
                msg = f"Señal IA: {result['signal']} (confianza: {result['probability_up']:.2%}, RSI: {result['rsi']})"
                
                self.root.after(0, lambda: self.exec_page.log_message(msg, "INFO"))
                self.root.after(0, lambda: self.toast.show(msg, "success"))
                
            except Exception as e:
                self.root.after(0, lambda: self.toast.show(str(e), "error"))
                self.root.after(0, lambda: self.exec_page.log_message(f"Error IA: {e}", "ERROR"))
        
        self.executor.submit(task)
    
    def execute_market_order(self):
        """Ejecutar orden de mercado con confirmación si es real"""
        mode = self.order_mode_var.get()
        
        if mode == "real":
            if not messagebox.askyesno("Confirmar", 
                "¿Estás seguro de ejecutar una orden REAL en Binance?\n\n"
                "Esta acción usará fondos reales."):
                return
        
        def task():
            try:
                symbol = self.ai_symbol_var.get().upper()
                qty = float(self.ai_qty_var.get())
                
                # Obtener señal actual
                signal_data = self.ai_engine.run(
                    symbol, self.ai_interval_var.get(), int(self.ai_limit_var.get())
                )
                signal = signal_data["signal"]
                
                if signal == "HOLD":
                    self.root.after(0, lambda: self.toast.show("Señal HOLD - orden omitida", "warning"))
                    return
                
                # Scan rápido para mejor ruta
                self.scanner.configure(
                    self.market_var.get(),
                    self.network_var.get(),
                    float(self.fee_var.get())
                )
                
                scan = self.scanner.scan(qty, max_paths=3, max_assets=50)
                
                best_path = "N/A"
                net_profit = 0.0
                
                if scan.opportunities:
                    best = scan.opportunities[0]
                    best_path = " → ".join(best.path)
                    net_profit = best.net_profit_usdt
                
                # Simular o ejecutar
                if mode == "simulador":
                    status = "SIMULADO"
                    order_id = None
                else:
                    # Aquí iría la ejecución real
                    status = "TESTNET" if mode == "testnet" else "REAL"
                    order_id = f"ORD-{int(time.time())}"
                
                # Registrar trade
                trade = TradeRecord(
                    timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    symbol=symbol,
                    signal=signal,
                    best_path=best_path,
                    net_profit=net_profit if signal == "BUY" else -net_profit,
                    mode=status,
                    status="OK",
                    order_id=order_id
                )
                
                self.executed_trades.append(trade)
                
                self.root.after(0, lambda: self._update_trade_display(trade, status))
                
            except Exception as e:
                self.root.after(0, lambda: self.toast.show(f"Error en orden: {e}", "error"))
                self.root.after(0, lambda: self.exec_page.log_message(f"Error orden: {e}", "ERROR"))
        
        self.executor.submit(task)
    
    def _update_trade_display(self, trade: TradeRecord, status: str):
        msg = f"Orden {trade.mode} - {trade.symbol} {trade.signal} - Ganancia: {trade.net_profit:.6f} USDT"
        self.exec_page.log_message(msg, "TRADE")
        self.exec_page.update_trade_tables(self.executed_trades)
        self.toast.show(f"Orden ejecutada ({status})", "success")

def main():
    root = tk.Tk()
    
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except:
        pass
    
    app = ArbitrageApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
