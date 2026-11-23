"""Syntax routing is separate from provider-verified security identity."""

import re

from elliottwaver.errors import InputError
from elliottwaver.schemas.market import Instrument, asset_type_for


def route_symbol(symbol: str) -> Instrument:
    text = symbol.strip().upper()
    match = re.fullmatch(r"(?:(SH|SZ|BJ)\.?)?(\d{6})(?:\.(SH|SZ|BJ))?", text)
    if match:
        before, digits, after = match.groups()
        if before and after and before != after:
            raise InputError("Conflicting symbol exchange prefix and suffix")
        if digits.startswith("880"):
            raise InputError("880xxx sector indices are not supported by the Sina index endpoint")
        if (before or after) == "SH" and digits.startswith("399"):
            raise InputError("399xxx is a Shenzhen index range; use SZ.399xxx")
        inferred = (
            "SH"
            if digits.startswith("6")
            else "SZ"
            if digits.startswith(("0", "3"))
            else "BJ"
            if digits.startswith(("4", "8", "920"))
            else None
        )
        exchange = before or after or inferred
        if not exchange:
            raise InputError("Cannot resolve this symbol to an unambiguous exchange")
        canonical = f"{digits}.{exchange}"
        asset_type = asset_type_for("CN", canonical)
        if asset_type != "INDEX" and inferred and inferred != exchange:
            raise InputError("Cannot resolve this symbol to an unambiguous exchange")
        return Instrument(
            instrument_id=f"CN:{canonical}",
            input_symbol=symbol,
            canonical_symbol=canonical,
            provider_symbol=f"{exchange.lower()}{digits}",
            market="CN",
            exchange=exchange,
            currency="CNY",
            timezone="Asia/Shanghai",
            tick_size=0.001 if asset_type == "INDEX" else 0.01,
        )
    if not re.fullmatch(r"[A-Z]{1,10}(?:[.\-][A-Z]{1,3})?", text):
        raise InputError(f"Unsupported symbol: {symbol!r}")
    # Only these explicitly supported share-class aliases are normalized.
    aliases = {"BRK-B": "BRK.B", "BRK-A": "BRK.A", "BF-B": "BF.B", "BF-A": "BF.A"}
    canonical = aliases.get(text, text)
    asset_type = asset_type_for("US", canonical)
    return Instrument(
        instrument_id=f"US_INDEX:{canonical}" if asset_type == "INDEX" else f"US:{canonical}",
        input_symbol=symbol,
        canonical_symbol=canonical,
        provider_symbol=canonical,
        market="US",
        exchange="US_INDEX" if asset_type == "INDEX" else "UNKNOWN",
        currency="USD",
        timezone="America/New_York",
        tick_size=0.01,
    )


def canonical_us_symbol(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        instrument = route_symbol(value)
    except InputError:
        return None
    return instrument.provider_symbol if instrument.market == "US" else None
