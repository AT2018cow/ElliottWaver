"""Syntax routing is separate from provider-verified security identity."""

import re

from eliotbot.errors import InputError
from eliotbot.schemas.market import Instrument


def route_symbol(symbol: str) -> Instrument:
    text = symbol.strip().upper()
    match = re.fullmatch(r"(?:(SH|SZ|BJ)\.?)?(\d{6})(?:\.(SH|SZ|BJ))?", text)
    if match:
        before, digits, after = match.groups()
        if before and after and before != after:
            raise InputError("Conflicting symbol exchange prefix and suffix")
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
        if not exchange or (inferred and inferred != exchange):
            raise InputError("Cannot resolve this stock code to an unambiguous exchange")
        return Instrument(
            instrument_id=f"CN:{digits}.{exchange}",
            input_symbol=symbol,
            canonical_symbol=f"{digits}.{exchange}",
            provider_symbol=f"{exchange.lower()}{digits}",
            market="CN",
            exchange=exchange,
            currency="CNY",
            timezone="Asia/Shanghai",
        )
    if not re.fullmatch(r"[A-Z]{1,10}(?:[.\-][A-Z]{1,3})?", text):
        raise InputError(f"Unsupported stock symbol: {symbol!r}")
    # Only these explicitly supported share-class aliases are normalized.
    aliases = {"BRK-B": "BRK.B", "BRK-A": "BRK.A", "BF-B": "BF.B", "BF-A": "BF.A"}
    canonical = aliases.get(text, text)
    return Instrument(
        instrument_id=f"US:{canonical}",
        input_symbol=symbol,
        canonical_symbol=canonical,
        provider_symbol=canonical,
        market="US",
        exchange="UNKNOWN",
        currency="USD",
        timezone="America/New_York",
    )
