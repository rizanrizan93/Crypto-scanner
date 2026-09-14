# ruff: noqa
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

UNIVERSE = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT",
    "ADAUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT", "LTCUSDT",
    "BCHUSDT", "DOTUSDT", "UNIUSDT", "AAVEUSDT", "NEARUSDT", "ETCUSDT",
    "FILUSDT", "ATOMUSDT",
)
SLUGS = {
    "BTCUSDT": "btc", "ETHUSDT": "eth", "SOLUSDT": "sol", "XRPUSDT": "xrp",
    "BNBUSDT": "bnb", "DOGEUSDT": "doge", "ADAUSDT": "ada", "TRXUSDT": "trx",
    "LINKUSDT": "link", "AVAXUSDT": "avax", "SUIUSDT": "sui", "LTCUSDT": "ltc",
    "BCHUSDT": "bch", "DOTUSDT": "dot", "UNIUSDT": "uni", "AAVEUSDT": "aave",
    "NEARUSDT": "near", "ETCUSDT": "etc", "FILUSDT": "fil", "ATOMUSDT": "atom",
}
START = date(2020, 1, 1)
END = date(2026, 9, 12)
REFERENCE_START = date(2012, 1, 1)
TRAIN_END = date(2022, 12, 31)
VALIDATION_END = date(2024, 12, 31)
BASE_COST_BPS = 14.0
BINANCE_API = "https://fapi.binance.com"
BINANCE_ARCHIVE = "https://data.binance.vision/data/futures/um"
CM_BASE = "https://raw.githubusercontent.com/coinmetrics/data/master/csv"
USER_AGENT = "crypto-scanner-strategy-lab/1.0"


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Backtest:
    returns: dict[date, float]
    exposure: dict[date, float]
    turnover: dict[date, float]
    funding_pnl: dict[date, float]


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _day_from_ts(raw: int) -> date:
    ts = raw / 1000
    if raw > 10**14:
        ts = raw / 1_000_000
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _month_iter(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y += 1
            m = 1


def _checked_zip(client: httpx.Client, url: str) -> bytes | None:
    r = client.get(url)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"archive fetch {r.status_code}: {url}")
    checksum = client.get(url + ".CHECKSUM")
    if checksum.status_code != 200:
        raise RuntimeError(f"checksum fetch {checksum.status_code}: {url}")
    parts = checksum.text.strip().split()
    if not parts or len(parts[0]) != 64:
        raise RuntimeError(f"invalid checksum manifest: {url}")
    if hashlib.sha256(r.content).hexdigest().lower() != parts[0].lower():
        raise RuntimeError(f"checksum mismatch: {url}")
    return r.content


def _zip_csv_rows(payload: bytes):
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if len(names) != 1:
            raise RuntimeError("archive must contain exactly one CSV")
        raw = zf.read(names[0]).decode("utf-8")
    return list(csv.reader(io.StringIO(raw)))


def _parse_kline_rows(rows) -> list[Bar]:
    out = []
    for row in rows:
        if not row or not row[0].isdigit() or len(row) < 6:
            continue
        d = _day_from_ts(int(row[0]))
        out.append(Bar(d, float(row[1]), float(row[2]), float(row[3]), float(row[4])))
    return out


def _fetch_klines_api(symbol: str) -> list[Bar]:
    rows = []
    cursor = _ms(START)
    end_ms = _ms(END + timedelta(days=1)) - 1
    with httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        while cursor <= end_ms:
            r = client.get(
                BINANCE_API + "/fapi/v1/klines",
                params={"symbol": symbol, "interval": "1d", "startTime": cursor,
                        "endTime": end_ms, "limit": 1500},
            )
            if r.status_code != 200:
                raise RuntimeError(f"Binance API status {r.status_code}")
            payload = r.json()
            if not payload:
                break
            rows.extend(payload)
            nxt = int(payload[-1][0]) + 86_400_000
            if nxt <= cursor:
                break
            cursor = nxt
            if len(payload) < 1500:
                break
    return _parse_kline_rows(rows)


def _fetch_klines_archive(symbol: str) -> list[Bar]:
    by_day = {}
    with httpx.Client(timeout=25, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
        for y, m in _month_iter(START, END):
            name = f"{symbol}-1d-{y:04d}-{m:02d}.zip"
            url = f"{BINANCE_ARCHIVE}/monthly/klines/{symbol}/1d/{name}"
            payload = _checked_zip(client, url)
            if payload is None:
                continue
            for bar in _parse_kline_rows(_zip_csv_rows(payload)):
                if START <= bar.day <= END:
                    by_day[bar.day] = bar
        last = max(by_day) if by_day else START - timedelta(days=1)
        d = max(last + timedelta(days=1), date(END.year, END.month, 1))
        while d <= END:
            name = f"{symbol}-1d-{d.isoformat()}.zip"
            url = f"{BINANCE_ARCHIVE}/daily/klines/{symbol}/1d/{name}"
            payload = _checked_zip(client, url)
            if payload is not None:
                for bar in _parse_kline_rows(_zip_csv_rows(payload)):
                    if START <= bar.day <= END:
                        by_day[bar.day] = bar
            d += timedelta(days=1)
    return [by_day[d] for d in sorted(by_day)]


def fetch_futures_bars(symbol: str) -> tuple[list[Bar], str]:
    try:
        bars = _fetch_klines_api(symbol)
        if bars:
            return bars, "BINANCE_FAPI_PUBLIC"
    except Exception:
        pass
    return _fetch_klines_archive(symbol), "BINANCE_VISION_CHECKSUM"


def _parse_funding_rows(rows) -> dict[date, float]:
    out = defaultdict(float)
    for row in rows:
        if not row:
            continue
        if row[0].isdigit():
            calc_time = int(row[0])
            rate = float(row[2]) if len(row) > 2 else float(row[-1])
        elif row[0] == "calc_time":
            continue
        else:
            continue
        out[_day_from_ts(calc_time)] += rate
    return dict(out)


def _fetch_funding_api(symbol: str) -> dict[date, float]:
    out = defaultdict(float)
    cursor = _ms(START)
    end_ms = _ms(END + timedelta(days=1)) - 1
    with httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        while cursor <= end_ms:
            r = client.get(
                BINANCE_API + "/fapi/v1/fundingRate",
                params={"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000},
            )
            if r.status_code != 200:
                raise RuntimeError(f"funding API status {r.status_code}")
            payload = r.json()
            if not payload:
                break
            for item in payload:
                out[_day_from_ts(int(item["fundingTime"]))] += float(item["fundingRate"])
            nxt = int(payload[-1]["fundingTime"]) + 1
            if nxt <= cursor:
                break
            cursor = nxt
            if len(payload) < 1000:
                break
    return dict(out)


def _fetch_funding_archive(symbol: str) -> dict[date, float]:
    out = defaultdict(float)
    with httpx.Client(timeout=25, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
        for y, m in _month_iter(START, END):
            name = f"{symbol}-fundingRate-{y:04d}-{m:02d}.zip"
            url = f"{BINANCE_ARCHIVE}/monthly/fundingRate/{symbol}/{name}"
            payload = _checked_zip(client, url)
            if payload is None:
                continue
            parsed = _parse_funding_rows(_zip_csv_rows(payload))
            for d, v in parsed.items():
                if START <= d <= END:
                    out[d] += v
    return dict(out)


def fetch_funding(symbol: str) -> tuple[dict[date, float], str]:
    try:
        rows = _fetch_funding_api(symbol)
        if rows:
            return rows, "BINANCE_FAPI_FUNDING"
    except Exception:
        pass
    return _fetch_funding_archive(symbol), "BINANCE_VISION_FUNDING_CHECKSUM"


def fetch_recent_oi(symbol: str) -> dict[date, float]:
    with httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        r = client.get(
            BINANCE_API + "/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "1d", "limit": 30},
        )
        if r.status_code != 200:
            return {}
        out = {}
        for item in r.json():
            out[_day_from_ts(int(item["timestamp"]))] = float(item["sumOpenInterestValue"])
        return out


def fetch_coinmetrics(symbol: str) -> list[Bar]:
    slug = SLUGS[symbol]
    url = f"{CM_BASE}/{slug}.csv"
    with httpx.Client(timeout=45, headers={"User-Agent": USER_AGENT}) as client:
        r = client.get(url)
        if r.status_code != 200:
            return []
    reader = csv.DictReader(io.StringIO(r.text))
    out = []
    for row in reader:
        try:
            d = date.fromisoformat(row["time"][:10])
        except Exception:
            continue
        if d < REFERENCE_START or d > END:
            continue
        raw = row.get("ReferenceRateUSD") or row.get("PriceUSD") or ""
        if not raw:
            raw = row.get("PriceUSD") or ""
        try:
            px = float(raw)
        except Exception:
            continue
        if px > 0:
            out.append(Bar(d, px, px, px, px))
    return out


def load_all_data():
    bars = {}
    funding = {}
    oi = {}
    bar_sources = {}
    funding_sources = {}
    print("DATA_FETCH_START", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_futures_bars, s): ("bar", s) for s in UNIVERSE}
        futures.update({pool.submit(fetch_funding, s): ("fund", s) for s in UNIVERSE})
        for fut in as_completed(futures):
            kind, symbol = futures[fut]
            value, source = fut.result()
            if kind == "bar":
                bars[symbol] = value
                bar_sources[symbol] = source
                print(f"BAR {symbol} {len(value)} {source}", flush=True)
            else:
                funding[symbol] = value
                funding_sources[symbol] = source
                print(f"FUND {symbol} {len(value)} {source}", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(fetch_recent_oi, s): s for s in UNIVERSE}
        for fut in as_completed(jobs):
            symbol = jobs[fut]
            try:
                oi[symbol] = fut.result()
            except Exception:
                oi[symbol] = {}
    cm = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(fetch_coinmetrics, s): s for s in UNIVERSE}
        for fut in as_completed(jobs):
            symbol = jobs[fut]
            try:
                cm[symbol] = fut.result()
            except Exception:
                cm[symbol] = []
            print(f"CM {symbol} {len(cm[symbol])}", flush=True)
    return bars, funding, oi, cm, bar_sources, funding_sources


class Market:
    def __init__(self, bars: dict[str, list[Bar]], funding: dict[str, dict[date, float]]):
        self.bars = {s: sorted(v, key=lambda b: b.day) for s, v in bars.items() if v}
        self.funding = funding
        self.index = {s: {b.day: i for i, b in enumerate(v)} for s, v in self.bars.items()}
        self.calendar = [b.day for b in self.bars.get("BTCUSDT", []) if START <= b.day <= END]

    def idx(self, s, d):
        return self.index.get(s, {}).get(d)

    def bar(self, s, d):
        i = self.idx(s, d)
        return None if i is None else self.bars[s][i]

    def ret_n(self, s, d, n):
        i = self.idx(s, d)
        if i is None or i < n:
            return None
        a, b = self.bars[s][i - n].close, self.bars[s][i].close
        return b / a - 1 if a > 0 else None

    def daily_returns(self, s, d, n):
        i = self.idx(s, d)
        if i is None or i < n:
            return []
        rows = self.bars[s][i - n:i + 1]
        return [rows[j].close / rows[j - 1].close - 1 for j in range(1, len(rows)) if rows[j - 1].close > 0]

    def vol(self, s, d, n=20):
        r = self.daily_returns(s, d, n)
        return statistics.pstdev(r) if len(r) >= max(10, n // 2) else None

    def ema(self, s, d, period):
        i = self.idx(s, d)
        if i is None or i < period - 1:
            return None
        vals = [b.close for b in self.bars[s][:i + 1]]
        seed = sum(vals[:period]) / period
        alpha = 2 / (period + 1)
        cur = seed
        for v in vals[period:]:
            cur = v * alpha + cur * (1 - alpha)
        return cur

    def beta(self, s, d, n=60):
        rs = self.daily_returns(s, d, n)
        rb = self.daily_returns("BTCUSDT", d, n)
        if len(rs) != len(rb) or len(rs) < 30:
            return None
        mb = statistics.fmean(rb)
        ms = statistics.fmean(rs)
        varb = statistics.fmean([(x - mb) ** 2 for x in rb])
        if varb <= 0:
            return None
        cov = statistics.fmean([(x - ms) * (y - mb) for x, y in zip(rs, rb)])
        return cov / varb


def _invvol_weights(m: Market, symbols: list[str], d: date, gross: float) -> dict[str, float]:
    raw = {}
    for s in symbols:
        v = m.vol(s, d, 20)
        if v and v > 1e-9:
            raw[s] = 1 / v
    total = sum(raw.values())
    return {s: gross * x / total for s, x in raw.items()} if total else {}


def cross_sectional_weights(m: Market, lookback: int | str) -> dict[date, dict[str, float]]:
    out, current = {}, {}
    for i, d in enumerate(m.calendar):
        if i == 0:
            continue
        signal = m.calendar[i - 1]
        if signal.weekday() == 6:
            scores = []
            for s in UNIVERSE:
                if m.idx(s, signal) is None or m.idx(s, signal) < 200:
                    continue
                if lookback == "blend":
                    r28, r60 = m.ret_n(s, signal, 28), m.ret_n(s, signal, 60)
                    score = None if r28 is None or r60 is None else 0.5 * r28 + 0.5 * r60
                else:
                    score = m.ret_n(s, signal, int(lookback))
                if score is not None:
                    scores.append((score, s))
            scores.sort()
            if len(scores) >= 6:
                shorts = [s for _, s in scores[:3]]
                longs = [s for _, s in scores[-3:]]
                lw = _invvol_weights(m, longs, signal, 0.5)
                sw = _invvol_weights(m, shorts, signal, 0.5)
                current = {**lw, **{s: -w for s, w in sw.items()}}
            else:
                current = {}
        out[d] = dict(current)
    return out


def rotation_weights(m: Market, hedged: bool) -> dict[date, dict[str, float]]:
    out, current = {}, {}
    for i, d in enumerate(m.calendar):
        if i == 0:
            continue
        signal = m.calendar[i - 1]
        if signal.weekday() == 6:
            scores = []
            for s in UNIVERSE:
                if s == "BTCUSDT" or m.idx(s, signal) is None or m.idx(s, signal) < 200:
                    continue
                r28, r60 = m.ret_n(s, signal, 28), m.ret_n(s, signal, 60)
                if r28 is not None and r60 is not None:
                    scores.append((0.5 * r28 + 0.5 * r60, s))
            scores.sort(reverse=True)
            longs = [s for _, s in scores[:3]]
            if len(longs) == 3:
                base = _invvol_weights(m, longs, signal, 1.0)
                if not hedged:
                    current = base
                else:
                    beta = 0.0
                    for s, w in base.items():
                        b = m.beta(s, signal, 60)
                        beta += w * max(0.0, b or 0.0)
                    beta = min(beta, 1.5)
                    scale = 1 / (1 + beta) if beta > 0 else 1.0
                    current = {s: w * scale for s, w in base.items()}
                    if beta > 0:
                        current["BTCUSDT"] = -beta * scale
            else:
                current = {}
        out[d] = dict(current)
    return out


def trend_weights(m: Market, kind: str) -> dict[date, dict[str, float]]:
    state = {s: 0 for s in UNIVERSE}
    out = {}
    if kind.startswith("vol20"):
        entry_n, exit_n, compression = 20, 10, 0.80
    elif kind.startswith("vol30"):
        entry_n, exit_n, compression = 30, 15, 0.85
    elif kind == "turtle20":
        entry_n, exit_n, compression = 20, 10, None
    else:
        entry_n, exit_n, compression = 55, 20, None

    for i, d in enumerate(m.calendar):
        if i == 0:
            continue
        signal = m.calendar[i - 1]
        for s in UNIVERSE:
            idx = m.idx(s, signal)
            need = max(200 if compression is not None else entry_n + 2, 120 if compression is not None else 0)
            if idx is None or idx < need:
                state[s] = 0
                continue
            bars = m.bars[s]
            bar = bars[idx]
            prior_entry = bars[idx - entry_n:idx]
            prior_exit = bars[idx - exit_n:idx]
            ema200 = m.ema(s, signal, 200) if compression is not None else None
            allow = True
            if compression is not None:
                v20, v120 = m.vol(s, signal, 20), m.vol(s, signal, 120)
                allow = bool(v20 and v120 and v120 > 0 and v20 / v120 <= compression)
            if state[s] == 0:
                if bar.close > max(x.high for x in prior_entry) and (ema200 is None or bar.close > ema200) and allow:
                    state[s] = 1
                elif bar.close < min(x.low for x in prior_entry) and (ema200 is None or bar.close < ema200) and allow:
                    state[s] = -1
            elif state[s] > 0:
                if bar.close < min(x.low for x in prior_exit) or (ema200 is not None and bar.close < ema200):
                    state[s] = 0
            else:
                if bar.close > max(x.high for x in prior_exit) or (ema200 is not None and bar.close > ema200):
                    state[s] = 0
        active = [s for s, side in state.items() if side]
        base = _invvol_weights(m, active, signal, 1.0)
        out[d] = {s: base[s] * state[s] for s in base}
    return out


def funding_dislocation_weights(m: Market, z_threshold: float) -> dict[date, dict[str, float]]:
    out = {}
    for i, d in enumerate(m.calendar):
        if i == 0:
            continue
        signal = m.calendar[i - 1]
        candidates = []
        for s in UNIVERSE:
            if m.idx(s, signal) is None or m.idx(s, signal) < 200:
                continue
            hist_days = [signal - timedelta(days=k) for k in range(59, -1, -1)]
            vals = [m.funding.get(s, {}).get(x) for x in hist_days]
            vals = [x for x in vals if x is not None]
            if len(vals) < 40:
                continue
            mean, sd = statistics.fmean(vals), statistics.pstdev(vals)
            today = m.funding.get(s, {}).get(signal)
            mom3 = m.ret_n(s, signal, 3)
            if today is None or mom3 is None or sd <= 1e-12:
                continue
            z = (today - mean) / sd
            if z <= -z_threshold and mom3 > 0:
                candidates.append((abs(z), s, 1))
            elif z >= z_threshold and mom3 < 0:
                candidates.append((abs(z), s, -1))
        candidates.sort(reverse=True)
        chosen = candidates[:4]
        syms = [s for _, s, _ in chosen]
        base = _invvol_weights(m, syms, signal, 1.0)
        out[d] = {s: base.get(s, 0) * side for _, s, side in chosen if s in base}
    return out


def _btc_regime(m: Market, d: date) -> str:
    idx = m.idx("BTCUSDT", d)
    if idx is None or idx < 200:
        return "TRANSITION"
    b = m.bars["BTCUSDT"][idx]
    ema = m.ema("BTCUSDT", d, 200)
    mom = m.ret_n("BTCUSDT", d, 60)
    if ema is not None and mom is not None and b.close > ema and mom > 0:
        return "BULL"
    if ema is not None and mom is not None and b.close < ema and mom < 0:
        return "BEAR"
    return "TRANSITION"


def _bull_switch_map(m: Market) -> dict[date, bool]:
    bars = m.bars.get("BTCUSDT", [])
    mode, trend, ranging = None, False, False
    out = {}
    for i in range(len(bars)):
        if i < 200:
            out[bars[i].day] = False
            continue
        closes = [x.close for x in bars]
        path = closes[i - 20:i + 1]
        travelled = sum(abs(path[j] - path[j - 1]) for j in range(1, len(path)))
        er = abs(path[-1] - path[0]) / travelled if travelled else 0
        if er >= 0.30:
            mode = "TREND"
        elif er <= 0.20:
            mode = "RANGE"
        close = closes[i]
        if close > max(closes[i - 55:i]):
            trend = True
        elif close < min(closes[i - 20:i]):
            trend = False
        w = closes[i - 19:i + 1]
        mean = statistics.fmean(w)
        sd = statistics.pstdev(w)
        if close < mean - 2 * sd:
            ranging = True
        elif close >= mean:
            ranging = False
        sma200 = statistics.fmean(closes[i - 199:i + 1])
        out[bars[i].day] = close > sma200 and ((mode == "TREND" and trend) or (mode == "RANGE" and ranging))
    return out


def regime_specialist_weights(m: Market) -> dict[date, dict[str, float]]:
    bull_switch = _bull_switch_map(m)
    bear_current = {}
    out = {}
    for i, d in enumerate(m.calendar):
        if i == 0:
            continue
        signal = m.calendar[i - 1]
        regime = _btc_regime(m, signal)
        if regime == "BULL":
            out[d] = {"BTCUSDT": 1.0} if bull_switch.get(signal, False) else {}
            continue
        if regime != "BEAR":
            out[d] = {}
            continue
        if signal.weekday() == 6 or not bear_current:
            scores = []
            for s in UNIVERSE:
                if m.idx(s, signal) is None or m.idx(s, signal) < 200:
                    continue
                r = m.ret_n(s, signal, 28)
                if r is not None:
                    scores.append((r, s))
            scores.sort()
            shorts = [s for _, s in scores[:3]]
            sw = _invvol_weights(m, shorts, signal, 0.25)
            bear_current = {s: -w for s, w in sw.items()}
        out[d] = dict(bear_current)
    return out


def backtest(m: Market, weights: dict[date, dict[str, float]], cost_bps: float) -> Backtest:
    returns, exposure, turnover, funding_pnl = {}, {}, {}, {}
    previous = {}
    for i, d in enumerate(m.calendar):
        if i == 0:
            continue
        prev_day = m.calendar[i - 1]
        target = weights.get(d, {})
        gross = 0.0
        for s, w in target.items():
            b0, b1 = m.bar(s, prev_day), m.bar(s, d)
            if b0 is None or b1 is None or b0.close <= 0:
                continue
            gross += w * (b1.close / b0.close - 1)
        f = -sum(w * m.funding.get(s, {}).get(d, 0.0) for s, w in target.items())
        keys = set(previous) | set(target)
        t = sum(abs(target.get(s, 0.0) - previous.get(s, 0.0)) for s in keys)
        cost = t * cost_bps / 10_000
        returns[d] = gross + f - cost
        exposure[d] = sum(abs(x) for x in target.values())
        turnover[d] = t
        funding_pnl[d] = f
        previous = dict(target)
    return Backtest(returns, exposure, turnover, funding_pnl)


def metrics(rows: dict[date, float], start: date | None = None, end: date | None = None):
    data = [(d, r) for d, r in sorted(rows.items()) if (start is None or d >= start) and (end is None or d <= end)]
    if len(data) < 2:
        return {"days": len(data), "total_return": 0.0, "cagr": 0.0, "max_dd": 0.0, "sharpe": 0.0, "sortino": 0.0, "calmar": 0.0, "positive_day_rate": 0.0}
    equity, peak, max_dd = 1.0, 1.0, 0.0
    vals = []
    for _, r in data:
        r = max(r, -0.999999)
        vals.append(r)
        equity *= 1 + r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1)
    span = max((data[-1][0] - data[0][0]).days, 1) / 365.25
    cagr = equity ** (1 / span) - 1 if equity > 0 else -1.0
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    downside = [min(0.0, r) for r in vals]
    dsd = math.sqrt(statistics.fmean([x * x for x in downside])) if downside else 0.0
    sharpe = mean / sd * math.sqrt(365) if sd > 1e-12 else 0.0
    sortino = mean / dsd * math.sqrt(365) if dsd > 1e-12 else 0.0
    return {
        "days": len(data), "start": data[0][0].isoformat(), "end": data[-1][0].isoformat(),
        "total_return": equity - 1, "cagr": cagr, "max_dd": max_dd, "sharpe": sharpe,
        "sortino": sortino, "calmar": cagr / abs(max_dd) if max_dd < -1e-12 else 0.0,
        "positive_day_rate": sum(r > 0 for r in vals) / len(vals),
    }


def corr(a: dict[date, float], b: dict[date, float], start: date, end: date) -> float | None:
    common = [d for d in a if d in b and start <= d <= end]
    if len(common) < 30:
        return None
    x, y = [a[d] for d in common], [b[d] for d in common]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sx, sy = statistics.pstdev(x), statistics.pstdev(y)
    if sx <= 1e-12 or sy <= 1e-12:
        return None
    return statistics.fmean([(u - mx) * (v - my) for u, v in zip(x, y)]) / (sx * sy)


def summarize_bt(bt: Backtest):
    return {
        "overall": metrics(bt.returns),
        "train": metrics(bt.returns, START, TRAIN_END),
        "validation": metrics(bt.returns, TRAIN_END + timedelta(days=1), VALIDATION_END),
        "oos": metrics(bt.returns, VALIDATION_END + timedelta(days=1), END),
        "avg_gross_exposure": statistics.fmean(bt.exposure.values()) if bt.exposure else 0.0,
        "annual_turnover": (sum(bt.turnover.values()) / max(len(bt.turnover), 1)) * 365,
        "funding_contribution": sum(bt.funding_pnl.values()),
    }


def adaptive_meta(strategy_returns: dict[str, dict[date, float]], calendar: list[date]):
    current = []
    last_month = None
    out = {}
    for d in calendar:
        if d <= START:
            continue
        ym = (d.year, d.month)
        if ym != last_month:
            last_month = ym
            look_start = d - timedelta(days=180)
            ranks = []
            for name, rows in strategy_returns.items():
                m = metrics(rows, look_start, d - timedelta(days=1))
                if m["days"] >= 120 and m["sharpe"] > 0:
                    ranks.append((m["sharpe"], name))
            ranks.sort(reverse=True)
            current = [name for _, name in ranks[:3]]
        out[d] = statistics.fmean([strategy_returns[n].get(d, 0.0) for n in current]) if current else 0.0
    return out


def regime_router(m: Market, strategy_returns: dict[str, dict[date, float]]):
    out = {}
    for i, d in enumerate(m.calendar):
        if i == 0:
            continue
        signal = m.calendar[i - 1]
        reg = _btc_regime(m, signal)
        if reg == "BULL":
            names = ["vol_breakout_20_10", "rs_rotation_btc_hedged"]
        elif reg == "BEAR":
            names = ["cross_mom_blend", "turtle_55_20"]
        else:
            names = ["funding_dislocation_z2"]
        vals = [strategy_returns[n].get(d, 0.0) for n in names if n in strategy_returns]
        out[d] = statistics.fmean(vals) if vals else 0.0
    return out


def oi_overlay_recent(m: Market, oi: dict[str, dict[date, float]]):
    weights = {}
    for i, d in enumerate(m.calendar):
        if i == 0:
            continue
        signal = m.calendar[i - 1]
        candidates = []
        for s in UNIVERSE:
            series = oi.get(s, {})
            days = sorted(x for x in series if x <= signal)
            if len(days) < 12:
                continue
            changes = []
            for a, b in zip(days[-12:-1], days[-11:]):
                if series[a] > 0:
                    changes.append(series[b] / series[a] - 1)
            if len(changes) < 8:
                continue
            latest_prev, latest = days[-2], days[-1]
            oi_change = series[latest] / series[latest_prev] - 1 if series[latest_prev] > 0 else 0
            mean, sd = statistics.fmean(changes), statistics.pstdev(changes)
            if sd <= 1e-12 or oi_change < mean + sd:
                continue
            hist_days = [signal - timedelta(days=k) for k in range(29, -1, -1)]
            fvals = [m.funding.get(s, {}).get(x) for x in hist_days]
            fvals = [x for x in fvals if x is not None]
            today = m.funding.get(s, {}).get(signal)
            mom3 = m.ret_n(s, signal, 3)
            if len(fvals) < 15 or today is None or mom3 is None:
                continue
            fm, fs = statistics.fmean(fvals), statistics.pstdev(fvals)
            if fs <= 1e-12:
                continue
            z = (today - fm) / fs
            if z <= -1.5 and mom3 > 0:
                candidates.append((abs(z), s, 1))
            elif z >= 1.5 and mom3 < 0:
                candidates.append((abs(z), s, -1))
        candidates.sort(reverse=True)
        chosen = candidates[:3]
        syms = [s for _, s, _ in chosen]
        base = _invvol_weights(m, syms, signal, 1.0)
        weights[d] = {s: base.get(s, 0) * side for _, s, side in chosen if s in base}
    return weights


def reference_tests(cm: dict[str, list[Bar]]):
    usable = {s: v for s, v in cm.items() if len(v) >= 100}
    if "BTCUSDT" not in usable:
        return {"status": "NO_BTC_REFERENCE"}
    ref_market = Market(usable, {s: {} for s in usable})
    ref_market.calendar = [b.day for b in usable["BTCUSDT"] if REFERENCE_START <= b.day <= END]
    refs = {}
    for name, wf in {
        "cross_mom_blend_close_proxy": lambda: cross_sectional_weights(ref_market, "blend"),
        "rs_rotation_btc_hedged_close_proxy": lambda: rotation_weights(ref_market, True),
        "btc_regime_specialist_close_proxy": lambda: regime_specialist_weights(ref_market),
    }.items():
        bt = backtest(ref_market, wf(), BASE_COST_BPS)
        active = [d for d, e in bt.exposure.items() if e > 0]
        refs[name] = {
            "metrics": metrics(bt.returns, REFERENCE_START, END),
            "first_active": min(active).isoformat() if active else None,
            "last_active": max(active).isoformat() if active else None,
            "note": "Coin Metrics close/reference-rate proxy; short legs before perpetual-futures era are non-executable reference only.",
        }
    return refs


def run_lab():
    bars, funding, oi, cm, bar_sources, funding_sources = load_all_data()
    if "BTCUSDT" not in bars or len(bars["BTCUSDT"]) < 500:
        raise RuntimeError("insufficient BTC futures history")
    market = Market(bars, funding)
    definitions: dict[str, Callable[[], dict[date, dict[str, float]]]] = {
        "cross_mom_28": lambda: cross_sectional_weights(market, 28),
        "cross_mom_60": lambda: cross_sectional_weights(market, 60),
        "cross_mom_blend": lambda: cross_sectional_weights(market, "blend"),
        "vol_breakout_20_10": lambda: trend_weights(market, "vol20"),
        "vol_breakout_30_15": lambda: trend_weights(market, "vol30"),
        "turtle_20_10": lambda: trend_weights(market, "turtle20"),
        "turtle_55_20": lambda: trend_weights(market, "turtle55"),
        "funding_dislocation_z1_5": lambda: funding_dislocation_weights(market, 1.5),
        "funding_dislocation_z2": lambda: funding_dislocation_weights(market, 2.0),
        "rs_rotation_unhedged": lambda: rotation_weights(market, False),
        "rs_rotation_btc_hedged": lambda: rotation_weights(market, True),
        "regime_specialist_current": lambda: regime_specialist_weights(market),
    }
    weights = {name: fn() for name, fn in definitions.items()}
    base_bt = {name: backtest(market, w, BASE_COST_BPS) for name, w in weights.items()}
    base_returns = {name: bt.returns for name, bt in base_bt.items()}
    baseline = base_returns["regime_specialist_current"]
    results = {}
    for name, w in weights.items():
        result = summarize_bt(base_bt[name])
        result["cost_stress"] = {
            str(cost): {
                "validation": metrics(backtest(market, w, cost).returns, TRAIN_END + timedelta(days=1), VALIDATION_END),
                "oos": metrics(backtest(market, w, cost).returns, VALIDATION_END + timedelta(days=1), END),
            }
            for cost in (8.0, 14.0, 25.0)
        }
        result["oos_corr_to_current_regime_specialist"] = corr(
            base_returns[name], baseline, VALIDATION_END + timedelta(days=1), END
        )
        results[name] = result

    candidate_names = [n for n in results if n != "regime_specialist_current"]
    validation_qualified = [
        n for n in candidate_names
        if results[n]["validation"]["sharpe"] > 0
        and results[n]["validation"]["cagr"] > 0
        and results[n]["validation"]["max_dd"] > -0.40
    ]
    qualified_returns = {n: base_returns[n] for n in validation_qualified}
    adaptive = adaptive_meta(qualified_returns, market.calendar) if qualified_returns else {}
    router = regime_router(market, base_returns)

    oiw = oi_overlay_recent(market, oi)
    oibt = backtest(market, oiw, BASE_COST_BPS)
    oi_days = sorted({d for s in oi.values() for d in s})

    correlation = {}
    for a in validation_qualified:
        correlation[a] = {}
        for b in validation_qualified:
            correlation[a][b] = corr(base_returns[a], base_returns[b], VALIDATION_END + timedelta(days=1), END)

    coverage = {}
    for s in UNIVERSE:
        b = bars.get(s, [])
        f = funding.get(s, {})
        coverage[s] = {
            "bars": len(b), "bar_start": b[0].day.isoformat() if b else None,
            "bar_end": b[-1].day.isoformat() if b else None, "bar_source": bar_sources.get(s),
            "funding_days": len(f), "funding_start": min(f).isoformat() if f else None,
            "funding_end": max(f).isoformat() if f else None, "funding_source": funding_sources.get(s),
            "oi_days_recent": len(oi.get(s, {})), "coinmetrics_days": len(cm.get(s, [])),
        }

    return {
        "research_only": True,
        "live_execution_mutated": False,
        "forward_demo_promotion_mutated": False,
        "as_of": END.isoformat(),
        "universe": list(UNIVERSE),
        "method": {
            "train": f"{START.isoformat()}/{TRAIN_END.isoformat()}",
            "validation": f"{(TRAIN_END + timedelta(days=1)).isoformat()}/{VALIDATION_END.isoformat()}",
            "oos": f"{(VALIDATION_END + timedelta(days=1)).isoformat()}/{END.isoformat()}",
            "base_cost_bps_per_unit_turnover": BASE_COST_BPS,
            "cost_stress_bps": [8, 14, 25],
            "signal_execution": "all signals use information through prior UTC daily close; weights become effective next day",
            "funding": "actual Binance USD-M historical funding summed by UTC day when available",
            "oi": "official API recent-window only; no synthetic historical OI backfill",
        },
        "coverage": coverage,
        "strategies": results,
        "validation_qualified": validation_qualified,
        "adaptive_top3_180d": {
            "overall": metrics(adaptive),
            "validation": metrics(adaptive, TRAIN_END + timedelta(days=1), VALIDATION_END),
            "oos": metrics(adaptive, VALIDATION_END + timedelta(days=1), END),
            "components": validation_qualified,
            "selection_rule": "monthly top-3 positive trailing-180d Sharpe using past data only",
        },
        "fixed_regime_router": {
            "overall": metrics(router),
            "validation": metrics(router, TRAIN_END + timedelta(days=1), VALIDATION_END),
            "oos": metrics(router, VALIDATION_END + timedelta(days=1), END),
            "mapping": {"BULL": ["vol_breakout_20_10", "rs_rotation_btc_hedged"],
                        "BEAR": ["cross_mom_blend", "turtle_55_20"],
                        "TRANSITION": ["funding_dislocation_z2"]},
        },
        "oi_confirmed_dislocation_recent": {
            "metrics": metrics(oibt.returns, min(oi_days) if oi_days else END, END),
            "oi_window_start": min(oi_days).isoformat() if oi_days else None,
            "oi_window_end": max(oi_days).isoformat() if oi_days else None,
            "status": "RECENT_SANITY_ONLY" if oi_days else "OI_UNAVAILABLE",
        },
        "oos_correlation_validation_qualified": correlation,
        "coinmetrics_reference_2012_2026": reference_tests(cm),
    }


def _compact(report):
    ranking = []
    for name, data in report["strategies"].items():
        o = data["oos"]
        v = data["validation"]
        ranking.append((o["sharpe"], name, v["sharpe"], o["cagr"], o["max_dd"]))
    ranking.sort(reverse=True)
    return {
        "validation_qualified": report["validation_qualified"],
        "oos_ranking": [
            {"strategy": n, "oos_sharpe": s, "validation_sharpe": vs, "oos_cagr": c, "oos_max_dd": dd}
            for s, n, vs, c, dd in ranking
        ],
        "adaptive_oos": report["adaptive_top3_180d"]["oos"],
        "router_oos": report["fixed_regime_router"]["oos"],
        "oi_recent": report["oi_confirmed_dislocation_recent"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="strategy_lab_results.json")
    args = parser.parse_args()
    started = time.time()
    report = run_lab()
    report["runtime_seconds"] = time.time() - started
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print("STRATEGY_LAB_SUMMARY=" + json.dumps(_compact(report), sort_keys=True), flush=True)
    print(f"STRATEGY_LAB_OUTPUT={args.output}", flush=True)


if __name__ == "__main__":
    main()
