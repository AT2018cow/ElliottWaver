from typing import Literal

Timeframe = Literal["1m", "5m", "15m", "30m", "60m", "1d", "1w"]
TIMEFRAMES = ("1m", "5m", "15m", "30m", "60m", "1d", "1w")
BACKGROUND_TIMEFRAMES = {
    "1m": "5m",
    "5m": "60m",
    "15m": "60m",
    "30m": "60m",
    "60m": "1d",
    "1d": "1w",
}


def normalize_timeframe(value: str) -> str:
    aliases = {"1h": "60m", "hour": "60m", "day": "1d", "week": "1w"}
    value = str(value).strip().lower()
    value = aliases.get(value, value)
    if value not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {value!r}; choose {', '.join(TIMEFRAMES)} or 1h")
    return value


def minutes(value: str) -> int | None:
    value = normalize_timeframe(value)
    return int(value[:-1]) if value.endswith("m") else None
