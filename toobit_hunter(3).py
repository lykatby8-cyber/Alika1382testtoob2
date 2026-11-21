import json
import os
import time
import statistics
from pathlib import Path
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.toobit.com"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "data" / "state.json"
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

# Scanner settings
MAX_24H_CHANGE_PCT = 7.0
MIN_24H_QUOTE_VOLUME_USDT = 100_000
MAX_SYMBOLS_PER_SCAN = 100
MIN_INITIAL_SCORE = 3
MIN_CONFIRM_SCORE = 5
MIN_VOLUME_SPIKE = 1.8
WATCH_TTL_MINUTES = 90
ALERT_COOLDOWN_MINUTES = 180
REQUEST_PAUSE_SECONDS = 0.15

STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD",
    "USDE", "USDS", "PYUSD", "USDP", "EUR", "GBP"
}

session = requests.Session()
session.headers.update({"User-Agent": "Toobit-PreMove-Hunter/1.1"})


def request_json(path, params=None):
    url = BASE_URL + path
    response = session.get(url, params=params, timeout=20)
    if response.status_code == 429:
        raise RuntimeError("Toobit rate limit (429); next scheduled scan will retry.")
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("code") not in (None, 200, "200"):
        raise RuntimeError(f"Toobit API error: {payload}")
    return payload


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct_change(new_value, old_value):
    if not old_value:
        return 0.0
    return (new_value / old_value - 1.0) * 100.0


def load_state():
    if not STATE_FILE.exists():
        return {"watch": {}, "last_alert": {}, "last_oi": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("watch", {})
        data.setdefault("last_alert", {})
        data.setdefault("last_oi", {})
        return data
    except Exception:
        print("WARNING: state.json unreadable; starting with empty watchlist.")
        return {"watch": {}, "last_alert": {}, "last_oi": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    temp_file.replace(STATE_FILE)


def notify_ntfy(title, message):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC secret is missing; notification skipped.")
        return False

    # Topic is supplied by the repository secret, not hard-coded in source.
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    response = requests.post(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "mag,chart_with_upwards_trend"
        },
        timeout=20
    )
    response.raise_for_status()
    print("ntfy notification sent.")
    return True


def get_contracts():
    info = request_json("/api/v1/exchangeInfo")
    contracts = info.get("contracts", []) if isinstance(info, dict) else []
    eligible = []

    for contract in contracts:
        symbol = str(contract.get("symbol", ""))
        underlying = str(contract.get("underlying", "")).upper()
        categories = {str(x).strip().lower() for x in (contract.get("categories") or [])}

        if contract.get("status") != "TRADING":
            continue
        if contract.get("inverse") is not False:
            continue
        if str(contract.get("marginToken", "")).upper() != "USDT":
            continue
        if str(contract.get("quoteAsset", "")).upper() != "USDT":
            continue
        if "tradfi" in categories:
            continue
        if underlying in STABLECOINS:
            continue
        if not symbol.endswith("-SWAP-USDT"):
            continue

        eligible.append(symbol)

    return sorted(set(eligible))


def get_all_tickers():
    data = request_json(
        "/quote/v1/contract/ticker/24hr",
        {"realtimeInterval": "24h"}
    )
    if not isinstance(data, list):
        raise RuntimeError("Unexpected 24h ticker response (expected list).")
    return {str(row.get("s")): row for row in data if row.get("s")}


def get_klines(symbol):
    rows = request_json(
        "/quote/v1/klines",
        {"symbol": symbol, "interval": "5m", "limit": 45}
    )
    result = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list) or len(row) < 11:
            continue
        result.append({
            "time": int(num(row[0])),
            "open": num(row[1]),
            "high": num(row[2]),
            "low": num(row[3]),
            "close": num(row[4]),
            "volume": num(row[5]),
            "quote_volume": num(row[7]),
            "taker_buy": num(row[9]),
            "taker_buy_quote": num(row[10])
        })
    return sorted(result, key=lambda x: x["time"])


def get_open_interest(symbol):
    data = request_json("/quote/v1/openInterest", {"symbol": symbol})
    items = data.get("openInterestList", []) if isinstance(data, dict) else []
    if not items:
        return 0.0
    return num(items[0].get("size"))


def get_funding_rates():
    data = request_json("/api/v1/futures/fundingRate")
    if not isinstance(data, list):
        return {}
    return {
        str(row.get("symbol")): num(row.get("rate"))
        for row in data if row.get("symbol")
    }


def candle_features(rows):
    if len(rows) < 22:
        return None

    current = rows[-1]             # Latest candle, potentially still forming
    previous = rows[-21:-1]       # 20 candles before current
    volumes = [r["volume"] for r in previous if r["volume"] > 0]
    if len(volumes) < 10:
        return None

    median_volume = statistics.median(volumes)
    volume_ratio = current["volume"] / median_volume if median_volume else 0.0

    recent_high = max(r["high"] for r in previous)
    recent_low = min(r["low"] for r in previous)
    midpoint = (recent_high + recent_low) / 2.0
    compression_pct = (
        (recent_high - recent_low) / midpoint * 100.0
        if midpoint else 999.0
    )

    candle_range = current["high"] - current["low"]
    close_position = (
        (current["close"] - current["low"]) / candle_range
        if candle_range > 0 else 0.5
    )

    taker_ratio = (
        current["taker_buy"] / current["volume"]
        if current["volume"] > 0 else 0.5
    )

    age_minutes = max(0.0, (time.time() * 1000 - current["time"]) / 60000.0)

    return {
        "current": current,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "volume_ratio": volume_ratio,
        "compression_pct": compression_pct,
        "close_position": close_position,
        "taker_ratio": taker_ratio,
        "age_minutes": age_minutes,
        "forming": age_minutes < 5.0
    }


def analyze(symbol, ticker, features, oi, previous_oi, funding):
    price = num(ticker.get("c"))
    change_24h = num(ticker.get("pcp"))
    quote_volume = num(ticker.get("qv"))
    if price <= 0:
        return None

    long_score = 0
    short_score = 0
    long_reasons = []
    short_reasons = []

    if abs(change_24h) <= MAX_24H_CHANGE_PCT:
        long_score += 1
        short_score += 1
        long_reasons.append("24h price change remains limited")
        short_reasons.append("24h price change remains limited")

    if features["volume_ratio"] >= MIN_VOLUME_SPIKE:
        long_score += 2
        short_score += 2
        reason = f"Current candle volume is {features['volume_ratio']:.2f}x 20-candle median"
        long_reasons.append(reason)
        short_reasons.append(reason)

    if features["compression_pct"] <= 3.0:
        long_score += 1
        short_score += 1
        reason = f"Recent range compression: {features['compression_pct']:.2f}%"
        long_reasons.append(reason)
        short_reasons.append(reason)

    distance_from_high = pct_change(price, features["recent_high"])
    distance_from_low = pct_change(price, features["recent_low"])

    if -2.0 <= distance_from_high <= 0.8:
        long_score += 1
        long_reasons.append("Price is near the recent range high")

    if -0.8 <= distance_from_low <= 2.0:
        short_score += 1
        short_reasons.append("Price is near the recent range low")

    if features["taker_ratio"] >= 0.58:
        long_score += 1
        long_reasons.append("Aggressive buying pressure")
    elif features["taker_ratio"] <= 0.42:
        short_score += 1
        short_reasons.append("Aggressive selling pressure")

    oi_change = None
    if previous_oi > 0 and oi > 0:
        oi_change = pct_change(oi, previous_oi)
        if oi_change >= 0.5:
            long_score += 1
            short_score += 1
            reason = f"Open Interest increased {oi_change:.2f}% since previous scan"
            long_reasons.append(reason)
            short_reasons.append(reason)

    candle = features["current"]
    if candle["close"] > candle["open"] and features["close_position"] >= 0.65:
        long_score += 1
        long_reasons.append("Live candle is bullish and closing near its high")
    elif candle["close"] < candle["open"] and features["close_position"] <= 0.35:
        short_score += 1
        short_reasons.append("Live candle is bearish and closing near its low")

    if long_score > short_score:
        direction, score, reasons = "LONG", long_score, long_reasons
    elif short_score > long_score:
        direction, score, reasons = "SHORT", short_score, short_reasons
    else:
        direction, score, reasons = "MIXED", max(long_score, short_score), []

    # Scenario levels are volatility-based monitoring references, not trade instructions.
    volatility_unit = max(
        (features["recent_high"] - features["recent_low"]) / 20.0,
        price * 0.003
    )

    if direction == "LONG":
        invalidation = min(features["recent_low"], price - volatility_unit)
        targets = [
            price + volatility_unit,
            price + 1.8 * volatility_unit,
            price + 2.7 * volatility_unit
        ]
    elif direction == "SHORT":
        invalidation = max(features["recent_high"], price + volatility_unit)
        targets = [
            price - volatility_unit,
            price - 1.8 * volatility_unit,
            price - 2.7 * volatility_unit
        ]
    else:
        invalidation, targets = None, []

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "price": price,
        "change24": change_24h,
        "quote_volume": quote_volume,
        "volume_ratio": features["volume_ratio"],
        "oi": oi,
        "oi_change": oi_change,
        "funding": funding,
        "forming": features["forming"],
        "candle_age": features["age_minutes"],
        "reasons": reasons,
        "invalidation": invalidation,
        "targets": targets
    }


def fmt(value):
    return "N/A" if value is None else f"{value:.10g}"


def alert_text(item):
    lines = [
        "TOOBIT PRE-MOVE HUNTER — CONFIRMED WATCH",
        "",
        f"Pair: {item['symbol']}",
        f"Bias: {item['direction']}",
        f"Score: {item['score']}",
        f"Last price: {fmt(item['price'])}",
        f"24h change: {item['change24']:.2f}%",
        f"Current candle volume / median: {item['volume_ratio']:.2f}x",
        f"Open Interest: {fmt(item['oi'])}",
        f"OI change since prior scan: {fmt(item['oi_change'])}%",
        f"Funding rate: {item['funding']:.8f}",
        f"Candle status: {'FORMING (not closed)' if item['forming'] else 'latest candle'}",
        f"Candle age: {item['candle_age']:.1f} minutes",
        "",
        "Reasons:"
    ]
    lines.extend(f"- {reason}" for reason in item["reasons"])
    if item["invalidation"] is not None:
        lines.extend([
            "",
            f"Scenario invalidation reference: {fmt(item['invalidation'])}",
            "Monitoring targets (not guaranteed):"
        ])
        lines.extend(
            f"Target {i}: {fmt(target)}"
            for i, target in enumerate(item["targets"], 1)
        )
    lines.extend([
        "",
        "Research alert only. Not a guaranteed prediction or entry instruction.",
        "No orders are placed by this program."
    ])
    return "\n".join(lines)


def main():
    print("=" * 60)
    print("TOOBIT CRYPTO PRE-MOVE HUNTER")
    print(datetime.now(timezone.utc).isoformat())
    print("=" * 60)

    state = load_state()
    now = time.time()

    contracts = get_contracts()
    tickers = get_all_tickers()
    funding = get_funding_rates()

    # First pass: liquid crypto USDT-M contracts with limited 24h movement.
    pool = []
    for symbol in contracts:
        ticker = tickers.get(symbol)
        if not ticker:
            continue
        if abs(num(ticker.get("pcp"))) > MAX_24H_CHANGE_PCT:
            continue
        if num(ticker.get("qv")) < MIN_24H_QUOTE_VOLUME_USDT:
            continue
        pool.append((num(ticker.get("qv")), symbol, ticker))

    pool.sort(reverse=True)
    selected = {symbol: ticker for _, symbol, ticker in pool[:MAX_SYMBOLS_PER_SCAN]}

    # Always re-check previously watched symbols, even if they leave the top-volume pool.
    for symbol in state["watch"]:
        if symbol in tickers:
            selected[symbol] = tickers[symbol]

    print(f"Eligible USDT-M crypto contracts: {len(contracts)}")
    print(f"Liquid pre-move candidates: {len(pool)}")
    print(f"Symbols being analyzed: {len(selected)}")

    next_watch = {}
    confirmed_alerts = []

    for symbol, ticker in selected.items():
        try:
            features = candle_features(get_klines(symbol))
            if features is None:
                print(f"{symbol}: insufficient candle data")
                continue

            oi = get_open_interest(symbol)
            old_oi = num(state["last_oi"].get(symbol))
            item = analyze(
                symbol, ticker, features, oi, old_oi,
                funding.get(symbol, 0.0)
            )
            state["last_oi"][symbol] = oi

            old = state["watch"].get(symbol)
            old_age = (
                (now - num(old.get("first_seen", now))) / 60.0
                if old else 0.0
            )

            if item and item["score"] >= MIN_INITIAL_SCORE and item["direction"] != "MIXED":
                next_watch[symbol] = {
                    "first_seen": old.get("first_seen", now) if old else now,
                    "last_seen": now,
                    "direction": item["direction"],
                    "best_score": max(
                        item["score"],
                        num(old.get("best_score")) if old else 0.0
                    )
                }

                # A candidate can only be confirmed on a later scheduled scan.
                if old and old.get("direction") == item["direction"]:
                    last_alert = num(state["last_alert"].get(symbol))
                    cooldown_min = (now - last_alert) / 60.0 if last_alert else 1e9

                    if item["score"] >= MIN_CONFIRM_SCORE and cooldown_min >= ALERT_COOLDOWN_MINUTES:
                        confirmed_alerts.append(item)
                        state["last_alert"][symbol] = now

            elif old and old_age <= WATCH_TTL_MINUTES:
                old["last_seen"] = now
                next_watch[symbol] = old

            print(
                f"{symbol}: {item['direction'] if item else 'NO DATA'} "
                f"score={item['score'] if item else '-'}"
            )

        except Exception as exc:
            print(f"SYMBOL ERROR {symbol}: {exc}")

        time.sleep(REQUEST_PAUSE_SECONDS)

    state["watch"] = next_watch
    save_state(state)

    print(f"Active watchlist: {len(next_watch)}")
    print(f"Confirmed signals: {len(confirmed_alerts)}")

    for item in confirmed_alerts:
        message = alert_text(item)
        print("\n" + message + "\n")
        try:
            notify_ntfy(
                f"Confirmed watch: {item['symbol']} {item['direction']}",
                message
            )
        except Exception as exc:
            print(f"NTFY ERROR for {item['symbol']}: {exc}")


if __name__ == "__main__":
    main()
