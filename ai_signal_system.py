"""AI-powered signal engine for crypto trading.

This module is designed for low-latency workflows:
- Uses Bybit public klines endpoint (no API key required for inference).
- Builds lightweight technical features.
- Trains an online logistic model (numpy SGD) to predict next-candle direction.
- Produces actionable signals with configurable confidence thresholds.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np
import pandas as pd
import requests


BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"


class MarketDataError(RuntimeError):
    """Raised when market data retrieval fails."""


@dataclass
class ModelConfig:
    learning_rate: float = 0.08
    l2: float = 1e-4
    epochs: int = 120
    threshold_buy: float = 0.58
    threshold_sell: float = 0.42


class BybitKlineClient:
    """Thin and efficient wrapper for Bybit klines."""

    def __init__(self, timeout: int = 8, retries: int = 3, pause_s: float = 0.25):
        self.timeout = timeout
        self.retries = retries
        self.pause_s = pause_s

    def fetch_klines(self, symbol: str, interval: str = "1", limit: int = 500) -> pd.DataFrame:
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = requests.get(BYBIT_KLINE_URL, params=params, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                if payload.get("retCode") != 0:
                    raise MarketDataError(f"Bybit API error: {payload}")

                rows = payload["result"]["list"]
                if not rows:
                    raise MarketDataError("Bybit returned empty kline list.")

                df = pd.DataFrame(
                    rows,
                    columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
                )

                numeric_cols = ["timestamp", "open", "high", "low", "close", "volume", "turnover"]
                for col in numeric_cols:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

                df = df.dropna().sort_values("timestamp").reset_index(drop=True)
                df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                return df
            except Exception as exc:  # noqa: BLE001 - collect and retry
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.pause_s)

        raise MarketDataError(f"Failed to fetch klines after retries: {last_error}")


class FeatureBuilder:
    """Builds compact, computation-friendly features."""

    @staticmethod
    def build(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, pd.DataFrame]:
        data = df.copy()

        data["ret_1"] = data["close"].pct_change(1)
        data["ret_3"] = data["close"].pct_change(3)
        data["ret_8"] = data["close"].pct_change(8)

        data["vol_z"] = (data["volume"] - data["volume"].rolling(20).mean()) / (
            data["volume"].rolling(20).std() + 1e-9
        )

        ema_fast = data["close"].ewm(span=9, adjust=False).mean()
        ema_slow = data["close"].ewm(span=21, adjust=False).mean()
        data["ema_gap"] = (ema_fast - ema_slow) / (ema_slow + 1e-9)

        rolling_std = data["close"].pct_change().rolling(20).std()
        data["volatility"] = rolling_std

        next_return = data["close"].shift(-1) / data["close"] - 1.0
        data["target"] = (next_return > 0).astype(int)

        feature_cols = ["ret_1", "ret_3", "ret_8", "vol_z", "ema_gap", "volatility"]
        model_df = data.dropna(subset=feature_cols + ["target"]).reset_index(drop=True)

        x = model_df[feature_cols].to_numpy(dtype=np.float64)
        y = model_df["target"].to_numpy(dtype=np.float64)
        return x, y, model_df


class OnlineLogisticModel:
    """Simple logistic regression with SGD, suitable for frequent retraining."""

    def __init__(self, n_features: int, config: ModelConfig):
        self.config = config
        self.weights = np.zeros(n_features, dtype=np.float64)
        self.bias = 0.0
        self.mean: np.ndarray | None = None
        self.std: np.ndarray | None = None

    @staticmethod
    def _sigmoid(z: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))

    def _normalize_fit(self, x: np.ndarray) -> np.ndarray:
        self.mean = x.mean(axis=0)
        self.std = x.std(axis=0) + 1e-9
        return (x - self.mean) / self.std

    def _normalize_transform(self, x: np.ndarray) -> np.ndarray:
        if self.mean is None or self.std is None:
            raise RuntimeError("Model normalization stats are not fitted.")
        return (x - self.mean) / self.std

    def fit(self, x: np.ndarray, y: np.ndarray) -> None:
        x_norm = self._normalize_fit(x)

        for _ in range(self.config.epochs):
            logits = x_norm @ self.weights + self.bias
            preds = self._sigmoid(logits)
            error = preds - y

            grad_w = (x_norm.T @ error) / len(x_norm) + self.config.l2 * self.weights
            grad_b = float(np.mean(error))

            self.weights -= self.config.learning_rate * grad_w
            self.bias -= self.config.learning_rate * grad_b

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        x_norm = self._normalize_transform(x)
        logits = x_norm @ self.weights + self.bias
        return self._sigmoid(logits)


@dataclass
class SignalResult:
    probability_up: float
    signal: str
    accuracy: float


class AISignalEngine:
    def __init__(self, config: ModelConfig | None = None):
        self.config = config or ModelConfig()

    def train_and_signal(self, x: np.ndarray, y: np.ndarray) -> SignalResult:
        if len(x) < 60:
            raise ValueError("Not enough rows to train robustly. Need at least 60 candles.")

        split = int(len(x) * 0.8)
        x_train, y_train = x[:split], y[:split]
        x_test, y_test = x[split:], y[split:]

        model = OnlineLogisticModel(n_features=x.shape[1], config=self.config)
        model.fit(x_train, y_train)

        test_probs = model.predict_proba(x_test)
        test_preds = (test_probs >= 0.5).astype(float)
        accuracy = float(np.mean(test_preds == y_test)) if len(y_test) else 0.0

        latest_prob = float(model.predict_proba(x[-1:].copy())[0])
        if latest_prob >= self.config.threshold_buy:
            signal = "BUY"
        elif latest_prob <= self.config.threshold_sell:
            signal = "SELL"
        else:
            signal = "HOLD"

        return SignalResult(probability_up=latest_prob, signal=signal, accuracy=accuracy)


def run(symbol: str, interval: str, limit: int) -> Dict[str, float | str]:
    data_client = BybitKlineClient()
    engine = AISignalEngine()

    df = data_client.fetch_klines(symbol=symbol, interval=interval, limit=limit)
    x, y, model_df = FeatureBuilder.build(df)
    result = engine.train_and_signal(x, y)

    last_close = float(model_df.iloc[-1]["close"])
    last_time = str(model_df.iloc[-1]["datetime"])

    return {
        "symbol": symbol,
        "interval": interval,
        "rows": int(len(model_df)),
        "last_close": last_close,
        "last_candle_utc": last_time,
        "test_accuracy": round(result.accuracy, 4),
        "probability_up": round(result.probability_up, 4),
        "signal": result.signal,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AI signal predictor for Bybit symbols")
    parser.add_argument("--symbol", default="BTCUSDT", help="Bybit symbol (e.g., BTCUSDT)")
    parser.add_argument("--interval", default="1", help="Kline interval (1, 3, 5, 15, etc.)")
    parser.add_argument("--limit", type=int, default=500, help="Number of candles to fetch")
    args = parser.parse_args()

    summary = run(symbol=args.symbol.upper(), interval=args.interval, limit=args.limit)
    print("=== AI SIGNAL SUMMARY ===")
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
