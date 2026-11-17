"""Explicit exchange sessions; no weekday approximation on the live path."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from importlib.metadata import version
from zoneinfo import ZoneInfo

from eliotbot.errors import DataError
from eliotbot.schemas.market import Instrument
from eliotbot.util import digest

XSHG_MIN_DATE = date(1991, 1, 2)


@dataclass(frozen=True)
class Session:
    date: date
    open_at: datetime
    close_at: datetime
    break_start: datetime | None = None
    break_end: datetime | None = None


@dataclass(frozen=True)
class TradingCalendar:
    sessions: tuple[Session, ...]
    version: str

    @property
    def dates(self) -> tuple[date, ...]:
        return tuple(s.date for s in self.sessions)

    def session(self, day: date) -> Session:
        result = next((s for s in self.sessions if s.date == day), None)
        if result is None:
            raise DataError(f"Date {day} is not in the verified exchange calendar")
        return result


def load_calendar(instrument: Instrument, start: date, end: date) -> TradingCalendar:
    try:
        import exchange_calendars as xcals
        import pandas as pd

        calendar = xcals.get_calendar(
            "XNYS" if instrument.market == "US" else "XSHG",
            start=(
                max(start - timedelta(days=50), XSHG_MIN_DATE)
                if instrument.market == "CN"
                else start - timedelta(days=50)
            ),
            end=end + timedelta(days=21),
        )
        labels = calendar.sessions_in_range(
            max(start - timedelta(days=40), XSHG_MIN_DATE)
            if instrument.market == "CN"
            else start - timedelta(days=40),
            end + timedelta(days=14),
        )
        sessions = tuple(
            Session(
                date=label.date(),
                open_at=calendar.session_open(label).to_pydatetime(),
                close_at=calendar.session_close(label).to_pydatetime(),
                break_start=calendar.session_break_start(label).to_pydatetime()
                if pd.notna(calendar.session_break_start(label))
                else None,
                break_end=calendar.session_break_end(label).to_pydatetime()
                if pd.notna(calendar.session_break_end(label))
                else None,
            )
            for label in labels
        )
        if not sessions:
            raise ValueError("calendar returned no sessions")
        identity = f"exchange-calendars/{version('exchange-calendars')}/{digest(sessions_payload(sessions))[:16]}"
        return TradingCalendar(sessions, identity)
    except (ImportError, ValueError, KeyError, OverflowError) as exc:
        raise DataError(
            "CALENDAR_UNAVAILABLE: install exchange-calendars and verify its date coverage"
        ) from exc


def sessions_payload(sessions):
    return [(s.date, s.open_at, s.close_at, s.break_start, s.break_end) for s in sessions]


def bar_window(
    session: Session, market: str, buffer_minutes: int = 20
) -> tuple[datetime, datetime, datetime]:
    if market == "US":
        zone = ZoneInfo("America/New_York")
        start = datetime.combine(session.date, time.min, zone)
        end = datetime.combine(session.date + timedelta(days=1), time.min, zone)
    else:
        start, end = session.open_at, session.close_at
    return (
        start.astimezone(UTC),
        end.astimezone(UTC),
        (end + timedelta(minutes=buffer_minutes)).astimezone(UTC),
    )


def latest_analyzable(
    calendar: TradingCalendar,
    instrument: Instrument,
    as_of: datetime,
    buffer_minutes: int = 20,
    *,
    through: date | None = None,
) -> date:
    eligible = [
        s.date
        for s in calendar.sessions
        if (through is None or s.date <= through)
        and bar_window(s, instrument.market, buffer_minutes)[2] <= as_of
    ]
    if not eligible:
        raise DataError("No completed, available session in the calendar coverage")
    return eligible[-1]


def parse_as_of(
    text: str | None, instrument: Instrument, now: datetime, buffer_minutes: int = 20
) -> datetime:
    if text is None:
        return now.astimezone(UTC)
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            if instrument.market == "US":
                result = datetime.combine(
                    day + timedelta(days=1), time.min, ZoneInfo(instrument.timezone)
                ) + timedelta(minutes=buffer_minutes)
            else:
                result = datetime.combine(day, time(15), ZoneInfo(instrument.timezone)) + timedelta(
                    minutes=buffer_minutes
                )
        else:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if result.tzinfo is None:
                raise ValueError("timestamp must include a timezone")
        if result > now:
            raise ValueError("requested session is not complete yet")
        return result.astimezone(UTC)
    except ValueError as exc:
        from eliotbot.errors import InputError

        raise InputError(f"Invalid --as-of: {exc}") from exc
