"""Download and cache free daily OHLCV data.
Stocks/ETFs: Yahoo Finance (yfinance, split/dividend adjusted).  Crypto: Binance public spot klines API.
Forex: currency ETFs FXE (euro) / FXB (pound) - Yahoo's raw FX daily bars (EURUSD=X) were rejected because
their OHLC is unreliable (Close frequently == Open), which corrupts any intraday strategy."""
import os, time, requests
import pandas as pd
import yfinance as yf

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
START = "2016-01-01"
END = "2026-11-03"  # last complete daily bar used (inclusive)

# Traded assets -> (asset class, pairs-trading partner)
ASSETS = {
    "SPY": ("equity_etf", "QQQ"),
    "QQQ": ("equity_etf", "SPY"),
    "AAPL": ("stock", "QQQ"),
    "GLD": ("commodity_etf", "SLV"),
    "BTCUSDT": ("crypto", "ETHUSDT"),
    "ETHUSDT": ("crypto", "BTCUSDT"),
    "FXE": ("forex", "FXB"),
}
EXTRA = ["SLV", "FXB"]
BINANCE = {"BTCUSDT", "ETHUSDT"}

# Round-trip-per-unit-turnover cost in basis points (fee + slippage), applied to |change in position|
COST_BPS = {"equity_etf": 5 + 5, "stock": 5 + 5, "commodity_etf": 5 + 5, "crypto": 10 + 10, "forex": 5 + 5}


def binance_daily(symbol):
    # data-api.binance.vision serves the same spot klines. api.binance.com returns HTTP 451 from US hosts.
    last = None
    start = int(pd.Timestamp(START).timestamp() * 1000)
    end = int((pd.Timestamp(END) + pd.Timedelta(days=1)).timestamp() * 1000) - 1
    for url in ("https://data-api.binance.vision/api/v3/klines", "https://api.binance.com/api/v3/klines"):
        rows, cursor = [], start
        try:
            while cursor < end:
                r = requests.get(url, params=dict(symbol=symbol, interval="1d", startTime=cursor, endTime=end, limit=1000), timeout=30)
                if r.status_code in (403, 418, 451):
                    raise RuntimeError(f"HTTP {r.status_code}")
                r.raise_for_status(); k = r.json()
                if isinstance(k, dict):
                    raise RuntimeError(k.get("msg") or k)
                if not k: break
                rows += k; cursor = k[-1][0] + 86400000; time.sleep(0.2)
            break
        except Exception as e:
            last = e; rows = []
            continue
    else:
        raise RuntimeError(f"binance daily {symbol} failed: {last}")
    if not rows:
        raise RuntimeError(f"binance daily {symbol} failed: {last}")
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["t", "Open", "High", "Low", "Close", "Volume"]
    df.index = pd.to_datetime(df.pop("t"), unit="ms"); df.index.name = "Date"
    return df.astype(float)


def download(ticker, end=None):
    path = os.path.join(DATA_DIR, ticker.replace("=", "_") + ".csv")
    if os.path.exists(path):
        return pd.read_csv(path, index_col=0, parse_dates=True)
    if ticker in BINANCE:
        df = binance_daily(ticker); df.to_csv(path); return df
    df = yf.download(ticker, start=START, end="2026-11-04", progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    df.to_csv(path)
    return df


def load_all():
    """Return dict ticker -> DataFrame aligned to a common business-day calendar (ffill)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    raw = {t: download(t) for t in list(ASSETS) + EXTRA}
    idx = pd.bdate_range(START, END)
    out = {}
    for t, df in raw.items():
        df = df[~df.index.duplicated()].reindex(idx)
        first = df["Close"].first_valid_index()
        df = df.loc[first:]
        mask = df["Close"].isna()
        df["Close"] = df["Close"].ffill()
        for c in ["Open", "High", "Low"]:
            df.loc[mask, c] = df.loc[mask, "Close"]  # flat bar on holidays
        df["Volume"] = df["Volume"].fillna(0)
        # FX volume from Yahoo is 0 -> use 1 so VWAP falls back to plain average
        if (df["Volume"] > 0).mean() < 0.5:
            df["Volume"] = 1.0
        out[t] = df.reindex(idx)  # NaN before inception
    return out


if __name__ == "__main__":
    d = load_all()
    for t, df in d.items():
        v = df.dropna()
        print(t, v.index.min().date(), v.index.max().date(), len(v))
