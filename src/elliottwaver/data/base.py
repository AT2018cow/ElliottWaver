"""Small provider boundary independent of the analysis engine."""

from datetime import date, datetime
from typing import Protocol

from elliottwaver.schemas.market import Instrument, MarketDataBundle


class DataProvider(Protocol):
    def fetch(
        self, instrument: Instrument, start: date, end: date, as_of: datetime, timeframe: str = "1d"
    ) -> MarketDataBundle: ...
