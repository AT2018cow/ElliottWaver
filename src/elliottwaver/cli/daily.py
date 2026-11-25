"""Local setup and defaults for the single-index CLI."""

from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml

from elliottwaver.config import Settings, read_settings_values, settings_from_values
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.periods import target_session
from elliottwaver.data.router import route_symbol
from elliottwaver.errors import InputError
from elliottwaver.schemas.market import Instrument
from elliottwaver.util import atomic_write
from elliottwaver.visualization.brief_text import feed_text

LOCAL_CONFIG = Path("config.local.yaml")
INDEX_NAMES = {
    "000001.SH": "上证指数",
    "000016.SH": "上证50",
    "000300.SH": "沪深300",
    "000688.SH": "科创50",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "899050.BJ": "北证50",
    "SPX": "标普500",
    "NDX": "纳斯达克100",
    "DJI": "道琼斯工业平均指数",
    "RUT": "罗素2000",
    "VIX": "标普500波动率指数",
}
DAILY_HELP = (
    "直接输入代码即可分析，例如 elliottwaver 000688.SH。\n"
    "自动查看日线，交易中加入可用的盘中判断，并提示与上次的变化。"
)
INDEX_FEEDS = {
    "CN": ("cn_index_intraday_feed", "tencent-cn-index"),
    "US": ("us_index_feed", "yahoo-index"),
}


def daily_config_path(config: str | Path | None) -> Path | None:
    if config is not None:
        return Path(config)
    return next((path for path in (LOCAL_CONFIG, Path("config.yaml")) if path.exists()), None)


def daily_instrument(
    symbol: str | None, default_index: str | None = None, *, index_only: bool = False
) -> Instrument:
    selected = symbol if symbol is not None else default_index
    if selected is None:
        raise InputError("请先运行 elliottwaver init 000688.SH，或在命令后指定指数代码。")
    instrument = route_symbol(selected)
    if (index_only or symbol is None) and instrument.asset_type != "INDEX":
        raise InputError(
            f"{instrument.canonical_symbol} 不是已支持的指数。沪市指数请带 .SH，例如 000688.SH。"
        )
    return instrument


def with_index_feed_defaults(settings: Settings, instrument: Instrument) -> Settings:
    if instrument.asset_type == "INDEX":
        key, default = INDEX_FEEDS[instrument.market]
        if not getattr(settings.data, key):
            settings = settings.model_copy(
                update={"data": settings.data.model_copy(update={key: default})}
            )
    return settings


def intraday_availability(
    instrument: Instrument, now: datetime, buffer_minutes: int
) -> Literal["CLOSED", "AWAITING_BAR", "AVAILABLE"]:
    day = now.astimezone(ZoneInfo(instrument.timezone)).date()
    calendar = load_calendar(instrument, day, day)
    session = next((item for item in calendar.sessions if item.date == day), None)
    if session is None or not session.open_at <= now < session.close_at:
        return "CLOSED"
    if (
        session.break_start is not None
        and session.break_end is not None
        and session.break_start <= now < session.break_end
    ):
        return "CLOSED"
    if target_session(calendar, instrument, now, "15m", day, buffer_minutes) != day:
        return "AWAITING_BAR"
    return "AVAILABLE"


def init_index(symbol: str, *, config: str | Path | None = None, yes: bool = False) -> None:
    instrument = daily_instrument(symbol, index_only=True)
    base = daily_config_path(config)
    values = read_settings_values(base)
    canonical = instrument.canonical_symbol
    values["default_index"] = canonical
    feed_key, default_feed = INDEX_FEEDS[instrument.market]
    data = values.setdefault("data", {})
    if not isinstance(data, dict):
        raise InputError("配置中的 data 必须是键值表。")
    if not data.get(feed_key):
        data[feed_key] = default_feed
    settings = settings_from_values(values)
    intraday_feed = getattr(settings.data, feed_key)
    daily_feed = "sina-index" if instrument.market == "CN" else intraday_feed
    label = f"{INDEX_NAMES[canonical]} · {canonical}" if canonical in INDEX_NAMES else canonical
    print(label)
    print(f"日线 {feed_text(daily_feed)} · 盘中 {feed_text(intraday_feed)}")
    if settings.default_index != canonical:
        print(f"当前环境变量覆盖默认指数 · {settings.default_index}")
    print(f"本地配置 · {LOCAL_CONFIG}")
    print(f"报告目录 · {settings.output.directory}")
    if not yes:
        try:
            confirmed = input("保存这份设置？ [y/N] ").strip().lower() in {"y", "yes"}
        except EOFError as exc:
            raise InputError("无交互输入。确认设置后可在 init 命令加 --yes。") from exc
        if not confirmed:
            print("已取消设置。")
            return
    try:
        atomic_write(LOCAL_CONFIG, yaml.safe_dump(values, allow_unicode=True, sort_keys=False))
    except OSError as exc:
        raise InputError(f"无法保存 {LOCAL_CONFIG} · {exc}") from exc
    print("已保存。接下来可运行 elliottwaver brief、intraday 或 history。")
