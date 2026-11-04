"""Price and daily-bar feeds for the paper engine.

api.binance.com returns HTTP 451 from US networks (this VM and GitHub-hosted
runners). BTC/ETH therefore prefer data-api.binance.vision, which serves the
same Binance spot klines the backtest was built on. Coinbase Exchange and
Kraken public APIs are fallbacks. Yahoo is tried first for stocks, ETFs and
GBP/USD, with the chart API and (for FX) open.er-api.com behind it.
"""
import json, math, os, time, datetime as dt
import pandas as pd
import requests

try:
    import yfinance as yf
except Exception:  # pragma: no cover - yfinance is required, but a missing import should not hide the chart fallback
    yf = None

from zoneinfo import ZoneInfo

VISION = "https://data-api.binance.vision"
BINANCE = "https://api.binance.com"
UA = "Mozilla/5.0 (compatible; paper-trading-bots/1.0; +https://github.com/mw9s2dhw72-ops/paper-trading-bots)"
ET = ZoneInfo("America/New_York")

YAHOO_TICKERS = ["SPY", "QQQ", "AAPL", "GLD", "FXE"]
# Binance symbol -> fallbacks. Kraken's result key varies (XXBTZUSD vs XBTUSD); we match loosely.
CRYPTO = {
    "BTCUSDT": {"asset": "BTC", "coinbase": "BTC-USD", "kraken": "XBTUSD"},
    "ETHUSDT": {"asset": "ETH", "coinbase": "ETH-USD", "kraken": "ETHUSD"},
}
MAX_MINUTE_LOOKBACK = 3 * 86400  # 1-minute stop lookback between scheduled runs


def log(*a):
    print(dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def http_json(url, params=None, timeout=20, tries=4):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": UA, "Accept": "application/json"})
            if r.status_code in (403, 418, 429, 451, 500, 502, 503, 520):
                last = f"HTTP {r.status_code}"
                time.sleep(0.8 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"{url} failed: {last}")


def _yf(fn, tries=3):
    if yf is None:
        raise RuntimeError("yfinance is not installed")
    last = None
    for i in range(tries):
        try:
            df = fn()
            if df is None or len(df) == 0:
                raise RuntimeError("empty Yahoo frame")
            return df
        except Exception as e:
            last = e
            log("yahoo retry", i + 1, e)
            time.sleep(1.2 * (i + 1))
    raise last


def _ohlc(df, ticker):
    """One ticker's Open/High/Low/Close/Volume, whichever MultiIndex layout yfinance returned."""
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = set(df.columns.get_level_values(0))
        lv1 = set(df.columns.get_level_values(1))
        if ticker in lv0:
            sub = df[ticker]
        elif ticker in lv1:
            sub = df.xs(ticker, axis=1, level=1)
        else:
            raise KeyError(ticker)
    else:
        sub = df
    cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in sub.columns]
    return sub[cols].copy()


def _naive_index(df):
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    return df


# ---------------------------------------------------------------- crypto daily
def _klines(base, symbol, interval, start_ms, end_ms=None, limit=1000):
    rows, safety = [], 0
    while safety < 20:
        safety += 1
        params = dict(symbol=symbol, interval=interval, startTime=start_ms, limit=limit)
        if end_ms is not None:
            params["endTime"] = end_ms
        k = http_json(f"{base}/api/v3/klines", params=params, tries=3)
        if isinstance(k, dict):
            raise RuntimeError(k.get("msg") or k)
        if not k:
            break
        rows.extend(k)
        nxt = k[-1][0] + 1
        if nxt <= start_ms or len(k) < limit:
            break
        start_ms = nxt
        if end_ms is not None and start_ms >= end_ms:
            break
    return rows


def _frame_from_klines(rows, completed_only=True):
    now_ms = time.time() * 1000
    if completed_only:
        rows = [r for r in rows if r[6] < now_ms]
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame([r[:6] for r in rows], columns=["t", "Open", "High", "Low", "Close", "Volume"])
    df.index = pd.to_datetime(df.pop("t"), unit="ms")
    df = df.astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[df.index.day_of_week < 5]


def _read_csv(symbol, data_dir):
    path = os.path.join(data_dir, f"{symbol}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _complete_utc_daily(df):
    """Drop the still-forming UTC day and weekend bars (crypto, same rule as the live engine)."""
    if df is None or len(df) == 0:
        return df
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    df = df[df.index + pd.Timedelta(days=1) <= now]
    return df[df.index.day_of_week < 5]


def _coinbase_daily(product):
    """Daily candles, oldest first. Coinbase returns [time, low, high, open, close, volume] newest first."""
    start = int(pd.Timestamp("2016-01-01", tz="UTC").timestamp())
    end = int(time.time())
    rows = []
    while start < end:
        chunk_end = min(end, start + 300 * 86400)
        k = http_json(f"https://api.exchange.coinbase.com/products/{product}/candles",
                      params={"granularity": 86400, "start": start, "end": chunk_end}, tries=3)
        if not isinstance(k, list) or not k:
            start = chunk_end
            continue
        rows.extend(k)
        start = chunk_end
    if not rows:
        raise RuntimeError("no coinbase daily candles")
    df = pd.DataFrame(rows, columns=["t", "Low", "High", "Open", "Close", "Volume"])
    df.index = pd.to_datetime(df.pop("t"), unit="s")
    df = df.astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    df = df[df.index + pd.Timedelta(days=1) <= now]
    return df[df.index.day_of_week < 5]


def _append_newer(base, extra):
    if base is None or len(base) == 0:
        return extra
    if extra is None or len(extra) == 0:
        return base
    extra = extra[extra.index > base.index.max()]
    if len(extra) == 0:
        return base
    return pd.concat([base, extra]).sort_index()


def crypto_daily(symbol, data_dir):
    """Completed weekday daily bars. Vision first (same series as the backtest CSV)."""
    start = int(pd.Timestamp("2016-01-01").timestamp() * 1000)
    for base in (VISION, BINANCE):
        try:
            rows = _klines(base, symbol, "1d", start)
            df = _frame_from_klines(rows)
            if len(df) > 100:
                return df
        except Exception as e:
            log(f"daily {symbol} via {base} failed:", e)
    cached = _complete_utc_daily(_read_csv(symbol, data_dir))
    product = CRYPTO[symbol]["coinbase"]
    try:
        extra = _coinbase_daily(product)
        merged = _append_newer(cached, extra)
        if merged is not None and len(merged):
            log(f"daily {symbol}: cache + Coinbase tail ({product})")
            return merged
    except Exception as e:
        log(f"coinbase daily {symbol} failed:", e)
    if cached is not None and len(cached):
        log(f"daily {symbol}: cached backtest CSV only")
        return cached
    raise RuntimeError(f"no daily history for {symbol}")


# ---------------------------------------------------------------- crypto live + 1m
def _vision_tickers():
    symbols = json.dumps(list(CRYPTO), separators=(",", ":"))
    data = http_json(f"{VISION}/api/v3/ticker/price", params={"symbols": symbols}, tries=3)
    if isinstance(data, dict) and "price" in data:
        data = [data]
    if not isinstance(data, list):
        raise RuntimeError(data)
    by_sym = {x["symbol"]: float(x["price"]) for x in data}
    t = time.time()
    return {CRYPTO[s]["asset"]: dict(price=by_sym[s], ts=t, src="Binance vision spot ticker (real-time)")
            for s in CRYPTO if s in by_sym}


def _coinbase_ticker(asset, product):
    data = http_json(f"https://api.exchange.coinbase.com/products/{product}/ticker", tries=3)
    return dict(price=float(data["price"]), ts=time.time(), src=f"Coinbase Exchange {product} ticker (fallback)")


def _kraken_tickers(assets):
    pairs = ",".join(CRYPTO[s]["kraken"] for s in CRYPTO if CRYPTO[s]["asset"] in assets)
    data = http_json("https://api.kraken.com/0/public/Ticker", params={"pair": pairs}, tries=3)
    if data.get("error"):
        raise RuntimeError(data["error"])
    result = data["result"]
    out = {}
    t = time.time()
    for sym, meta in CRYPTO.items():
        if meta["asset"] not in assets:
            continue
        key = next((k for k in result if meta["kraken"] in k or k.endswith(meta["kraken"])), None)
        if key is None and len(result) == 1 and len(assets) == 1:
            key = next(iter(result))
        if not key:
            continue
        out[meta["asset"]] = dict(price=float(result[key]["c"][0]), ts=t, src=f"Kraken {meta['kraken']} ticker (fallback)")
    return out


def live_crypto_prices():
    out, err = {}, None
    try:
        out.update(_vision_tickers())
    except Exception as e:
        err = e
        log("binance vision ticker failed:", e)
    missing = [m["asset"] for m in CRYPTO.values() if m["asset"] not in out]
    for sym, meta in CRYPTO.items():
        if meta["asset"] not in missing:
            continue
        try:
            out[meta["asset"]] = _coinbase_ticker(meta["asset"], meta["coinbase"])
        except Exception as e:
            err = e
            log(f"coinbase ticker {meta['coinbase']} failed:", e)
    missing = [a for a in (m["asset"] for m in CRYPTO.values()) if a not in out]
    if missing:
        try:
            out.update(_kraken_tickers(missing))
        except Exception as e:
            err = e
            log("kraken ticker failed:", e)
    if len(out) < len(CRYPTO):
        raise RuntimeError(f"crypto prices incomplete ({sorted(out)}); last error: {err}")
    return out


def _clean_bars(bars):
    """Drop non-positive or absurd 1-minute prints so a bad tick cannot stop out the book."""
    out = []
    for ts, o, h, l, c in bars:
        try:
            o, h, l, c = float(o), float(h), float(l), float(c)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(x) for x in (o, h, l, c)):
            continue
        if min(o, h, l, c) <= 0 or l > h:
            continue
        if l > 0 and h / l > 1.25:  # wider than 25% in one minute: treat as a bad print
            continue
        out.append((float(ts), o, h, l, c))
    return out


def _bars_from_klines(rows, since_ts):
    bars = []
    for r in rows:
        ts = r[0] / 1000.0
        if ts < since_ts - 60:
            continue
        o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
        bars.append((ts, o, h, l, c))
    bars.sort()
    return _clean_bars(bars)


def _crypto_minute_bars_one(symbol, since_ts):
    start_ms = int((since_ts - 60) * 1000)
    end_ms = int(time.time() * 1000)
    try:
        rows = _klines(VISION, symbol, "1m", start_ms, end_ms)
        if rows:
            return _bars_from_klines(rows, since_ts)
    except Exception as e:
        log(f"vision 1m {symbol} failed:", e)
    # Coinbase: max 300 candles per call, newest first, time in seconds
    product = CRYPTO[symbol]["coinbase"]
    bars, cursor_end = [], int(time.time())
    floor = int(since_ts - 60)
    while cursor_end > floor and len(bars) < 5000:
        chunk_start = max(floor, cursor_end - 300 * 60)
        k = http_json(f"https://api.exchange.coinbase.com/products/{product}/candles",
                      params={"granularity": 60, "start": chunk_start, "end": cursor_end}, tries=3)
        if not isinstance(k, list) or not k:
            break
        for row in k:
            ts, low, high, o, c = row[0], row[1], row[2], row[3], row[4]
            if ts >= since_ts - 60:
                bars.append((float(ts), float(o), float(high), float(low), float(c)))
        cursor_end = chunk_start
    bars.sort()
    return _clean_bars(bars)


def crypto_minute_bars(since_ts):
    """asset -> [(ts, open, high, low, close), ...] since since_ts (capped lookback)."""
    if since_ts is None:
        return {}
    since_ts = max(float(since_ts), time.time() - MAX_MINUTE_LOOKBACK)
    out = {}
    for symbol, meta in CRYPTO.items():
        try:
            out[meta["asset"]] = _crypto_minute_bars_one(symbol, since_ts)
        except Exception as e:
            log(f"1m bars {symbol} failed:", e)
    return out


# ---------------------------------------------------------------- yahoo
def _drop_incomplete_yahoo(df):
    """Match paper_engine.yahoo_daily: drop today's bar until 16:15 America/New_York."""
    df = _naive_index(df).dropna(subset=["Close"])
    now = dt.datetime.now(ET)
    today = pd.Timestamp(now.date())
    if not (now.hour > 16 or (now.hour == 16 and now.minute >= 15)):
        df = df[df.index < today]
    return df


def yahoo_daily_history(data_dir):
    def fetch():
        return yf.download(YAHOO_TICKERS, start="2016-01-01", interval="1d", auto_adjust=True,
                           progress=False, group_by="ticker")
    try:
        d = _yf(fetch, tries=3)
        out = {}
        for a in YAHOO_TICKERS:
            out[a] = _drop_incomplete_yahoo(_ohlc(d, a))
        if all(len(out[a]) > 100 for a in YAHOO_TICKERS):
            return out
        raise RuntimeError("yahoo daily frame too short")
    except Exception as e:
        log("yahoo daily download failed, using backtest CSV:", e)
    out = {}
    for a in YAHOO_TICKERS:
        df = _read_csv(a, data_dir)
        if df is None or len(df) == 0:
            raise RuntimeError(f"no daily history for {a}")
        out[a] = _drop_incomplete_yahoo(df)
    return out


def _bars_from_ohlc(df, since_ts):
    if since_ts is None or df is None or len(df) == 0:
        return []
    bars = []
    idx = pd.to_datetime(df.index)
    # timestamp() needs tz; Yahoo minute indexes are tz-aware. Localize naive as UTC.
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    for i, ts in enumerate(idx):
        tsf = ts.timestamp()
        if tsf < since_ts - 60:
            continue
        row = df.iloc[i]
        try:
            o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
        except Exception:
            continue
        if pd.isna(c) or pd.isna(h) or pd.isna(l):
            continue
        if pd.isna(o):
            o = c
        bars.append((tsf, o, h, l, c))
    return _clean_bars(bars)


def _prices_from_minute(reg, ext):
    out = {}
    for a in YAHOO_TICKERS:
        try:
            sub = _ohlc(reg, a).dropna(subset=["Close"])
        except Exception:
            continue
        if len(sub) == 0:
            continue
        close = sub["Close"]
        ts = pd.to_datetime(close.index)
        if getattr(ts, "tz", None) is None:
            ts = ts.tz_localize("UTC")
        rec = dict(price=float(close.iloc[-1]), ts=float(ts[-1].timestamp()),
                   src="Yahoo Finance 1-min (unofficial)")
        if ext is not None:
            try:
                e = _ohlc(ext, a)["Close"].dropna()
                if len(e):
                    et = pd.to_datetime(e.index)
                    if getattr(et, "tz", None) is None:
                        et = et.tz_localize("UTC")
                    rec["ext_price"] = float(e.iloc[-1])
                    rec["ext_ts"] = float(et[-1].timestamp())
            except Exception:
                pass
        out[a] = rec
    if len(out) < len(YAHOO_TICKERS):
        raise RuntimeError(f"yahoo minute prices missing {sorted(set(YAHOO_TICKERS) - set(out))}")
    return out


def _chart_intraday(since_ts):
    """query1/query2 chart API, one symbol at a time. Used when yfinance fails."""
    prices, bars = {}, {}
    hosts = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
    for a in YAHOO_TICKERS:
        last = None
        for host in hosts:
            try:
                j = http_json(f"{host}/v8/finance/chart/{a}",
                              params={"interval": "1m", "range": "5d", "includePrePost": "false"}, tries=2)
                result = j["chart"]["result"][0]
                ts = result.get("timestamp") or []
                q = result["indicators"]["quote"][0]
                if not ts:
                    raise RuntimeError("no timestamps")
                idx = pd.to_datetime(ts, unit="s", utc=True)
                df = pd.DataFrame({
                    "Open": q["open"], "High": q["high"], "Low": q["low"], "Close": q["close"],
                    "Volume": q.get("volume") or [0] * len(ts),
                }, index=idx).dropna(subset=["Close"])
                close = df["Close"]
                prices[a] = dict(price=float(close.iloc[-1]), ts=float(close.index[-1].timestamp()),
                                 src="Yahoo chart API 1-min (fallback)")
                bars[a] = _bars_from_ohlc(df, since_ts)
                last = None
                break
            except Exception as e:
                last = e
        if a not in prices:
            log(f"yahoo chart {a} failed:", last)
    # extended hours, best effort, for the label only
    for a in list(prices):
        try:
            j = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{a}",
                          params={"interval": "1m", "range": "1d", "includePrePost": "true"}, tries=2)
            result = j["chart"]["result"][0]
            ts = result.get("timestamp") or []
            c = result["indicators"]["quote"][0]["close"]
            pairs = [(t, p) for t, p in zip(ts, c) if p is not None]
            if pairs:
                prices[a]["ext_price"] = float(pairs[-1][1])
                prices[a]["ext_ts"] = float(pairs[-1][0])
        except Exception:
            pass
    if len(prices) < len(YAHOO_TICKERS):
        raise RuntimeError(f"yahoo chart prices missing {sorted(set(YAHOO_TICKERS) - set(prices))}")
    return prices, bars


def yahoo_intraday(since_ts=None):
    """(prices, minute_bars). minute_bars is empty when since_ts is None."""
    try:
        reg = _yf(lambda: yf.download(YAHOO_TICKERS, period="5d", interval="1m", prepost=False,
                                      auto_adjust=True, progress=False), tries=3)
        ext = None
        try:
            ext = _yf(lambda: yf.download(YAHOO_TICKERS, period="1d", interval="1m", prepost=True,
                                          auto_adjust=True, progress=False), tries=2)
        except Exception as e:
            log("yahoo extended-hours prices skipped:", e)
        prices = _prices_from_minute(reg, ext)
        bars = {}
        if since_ts is not None:
            since = max(float(since_ts), time.time() - MAX_MINUTE_LOOKBACK)
            for a in YAHOO_TICKERS:
                try:
                    bars[a] = _bars_from_ohlc(_ohlc(reg, a), since)
                except Exception as e:
                    log(f"yahoo 1m bars {a} skipped:", e)
        return prices, bars
    except Exception as e:
        log("yfinance intraday failed, trying chart API:", e)
        return _chart_intraday(since_ts)


def _yahoo_fx():
    def fetch():
        return yf.download("GBPUSD=X", period="1d", interval="1m", progress=False, auto_adjust=True)
    df = _yf(fetch, tries=3)
    sub = _ohlc(df, "GBPUSD=X") if isinstance(df.columns, pd.MultiIndex) else df
    s = sub["Close"].dropna()
    if hasattr(s, "columns"):
        s = s.iloc[:, 0]
    if len(s) == 0:
        raise RuntimeError("empty GBPUSD series")
    ts = pd.to_datetime(s.index)
    if getattr(ts, "tz", None) is None:
        ts = ts.tz_localize("UTC")
    return dict(rate=float(s.iloc[-1]), ts=float(ts[-1].timestamp()), src="Yahoo GBPUSD=X 1-min (unofficial)")


def _chart_fx():
    j = http_json("https://query1.finance.yahoo.com/v8/finance/chart/GBPUSD=X",
                  params={"interval": "1m", "range": "1d"}, tries=3)
    result = j["chart"]["result"][0]
    ts = result.get("timestamp") or []
    c = result["indicators"]["quote"][0]["close"]
    pairs = [(t, p) for t, p in zip(ts, c) if p is not None]
    if not pairs:
        raise RuntimeError("empty GBPUSD chart")
    return dict(rate=float(pairs[-1][1]), ts=float(pairs[-1][0]), src="Yahoo chart GBPUSD=X 1-min (fallback)")


def _erapi_fx():
    r = http_json("https://open.er-api.com/v6/latest/GBP", tries=3)
    return dict(rate=float(r["rates"]["USD"]), ts=float(r["time_last_update_unix"]),
                src="open.er-api.com daily (fallback)")


def live_fx():
    """GBP/USD (USD per 1 GBP). Yahoo minute, then chart API, then open.er-api.com."""
    for fn in (_yahoo_fx, _chart_fx, _erapi_fx):
        try:
            return fn()
        except Exception as e:
            log(f"fx source {fn.__name__} failed:", e)
    raise RuntimeError("no GBP/USD rate")
