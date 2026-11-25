"""Validated configuration; credentials never enter the analysis fingerprint."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from elliottwaver.errors import InputError
from elliottwaver.schemas.index_plan import IndexSizingInput
from elliottwaver.timeframes import Timeframe, normalize_timeframe
from elliottwaver.util import digest


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class AnalysisConfig(Section):
    timeframe: Timeframe = "1d"
    engine_profile: Literal["classic_v4", "hierarchical_v1"] = "classic_v4"
    top_k: int = Field(3, ge=1, le=20)
    history_years: int = Field(5, ge=1, le=20)
    min_anchor_bars: int = Field(60, ge=6)
    max_analysis_bars: int = Field(600, ge=60, le=5000)
    max_anchors: int = Field(4, ge=1, le=8)
    anchor_mode: Literal["recent", "spread"] = "recent"
    warmup_bars: int = Field(130, ge=26)
    scales: tuple[float, ...] = (1.4, 2.4, 3.8)
    macro_scale: float = Field(4.0, gt=0)

    @field_validator("timeframe", mode="before")
    @classmethod
    def normalize_period(cls, value):
        return normalize_timeframe(value)

    @model_validator(mode="after")
    def bounds(self):
        if not self.scales or len(self.scales) > 8 or any(k <= 0 for k in self.scales):
            raise ValueError("scales must contain 1–8 positive thresholds")
        if len(set(self.scales)) != len(self.scales):
            raise ValueError("scales must be unique")
        if self.min_anchor_bars > self.max_analysis_bars:
            raise ValueError("min_anchor_bars exceeds max_analysis_bars")
        return self


class SearchConfig(Section):
    beam_width: int = Field(128, ge=1, le=1024)
    max_expansions: int = Field(50000, ge=1, le=1000000)
    max_bridge_intervals: int = Field(5, ge=1, le=21)
    max_subwave_depth: int = Field(0, ge=0, le=2)

    @field_validator("max_bridge_intervals")
    @classmethod
    def odd_bridge_intervals(cls, value):
        if value % 2 == 0:
            raise ValueError("max_bridge_intervals must be odd")
        return value


ScoringProfile = Literal["ew_score_v1", "ew_score_v2"]


class ScoringConfig(Section):
    profile: ScoringProfile = "ew_score_v2"
    min_quality: float = Field(40, ge=0, le=100)
    unclear_below: float = Field(45, ge=0, le=100)
    min_coverage: float = Field(0.70, ge=0, le=1)
    temperature: float = Field(12, gt=0)


class DataConfig(Section):
    us_feed: Literal["sip", "iex"] = "sip"
    us_equity_feed: Literal["alpaca", "yahoo"] = "alpaca"
    us_index_feed: Literal["", "massive-index", "yahoo-index"] = ""
    cn_equity_intraday_feed: Literal[
        "sina", "tushare-cn-equity", "tencent-cn-equity", "baostock"
    ] = "sina"
    cn_index_intraday_feed: Literal["", "tushare-cn-index", "tencent-cn-index"] = ""
    request_timeout_seconds: float = Field(20, gt=0, le=60)
    max_attempts: int = Field(3, ge=1, le=5)
    total_fetch_deadline_seconds: float = Field(120, gt=0, le=600)
    max_requests_per_second: float = Field(2, gt=0, le=8)
    delayed_access_buffer_minutes: int = Field(20, ge=15, le=120)
    cache_ttl_seconds: int = Field(21600, ge=0)
    tencent_max_pages: int = Field(20, ge=1, le=200)


class IndexGuidanceConfig(Section):
    enabled: bool = True
    short_sessions: int = Field(5, ge=2, le=5)
    swing_sessions: int = Field(20, ge=10, le=30)
    activation_sessions: int = Field(2, ge=1, le=5)
    daily_trigger: Literal["close"] = "close"
    near_atr: float = Field(0.5, ge=0)
    max_chase_atr: float = Field(0.5, gt=0)
    near_tie_score: float = Field(3.0, ge=0, le=100)
    near_tie_min_coverage: float = Field(0.7, ge=0, le=1)
    sizing: dict[str, IndexSizingInput] = Field(default_factory=dict)
    market_context: bool = True
    event_context: bool = True
    references: dict[str, str] = Field(default_factory=dict)
    context_files: dict[str, Path] = Field(default_factory=dict)
    context_sessions: int = Field(20, ge=2, le=60)
    min_volatility_windows: int = Field(20, ge=2, le=500)
    breadth_min_coverage: float = Field(0.7, ge=0, le=1)
    intraday_tracking: bool = True
    intraday_trigger_timeframe: Literal["15m"] = "15m"
    intraday_trigger: Literal["TOUCH", "CLOSE", "RETEST"] = "CLOSE"
    retest_tolerance_points: float = Field(0, ge=0)
    intraday_max_age_minutes: int = Field(45, ge=15, le=240)
    background_max_age_minutes: int = Field(180, ge=60, le=480)
    volume_history_sessions: int = Field(20, ge=3, le=60)
    min_volume_sessions: int = Field(5, ge=3, le=60)

    @model_validator(mode="after")
    def volume_window(self):
        if self.min_volume_sessions > self.volume_history_sessions:
            raise ValueError("minimum volume sessions exceeds the history window")
        return self

    @property
    def policy_hash(self) -> str:
        fields = {
            "short_sessions",
            "swing_sessions",
            "activation_sessions",
            "daily_trigger",
            "near_atr",
            "max_chase_atr",
            "near_tie_score",
            "near_tie_min_coverage",
        }
        return digest(["single-index-daily-v1", self.model_dump(include=fields)])


class CacheConfig(Section):
    directory: Path = Path(".cache/elliottwaver")


class OutputConfig(Section):
    directory: Path = Path("reports")
    formats: tuple[Literal["html", "md", "png"], ...] = ("html", "md")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ELLIOTTWAVER_",
        env_nested_delimiter="__",
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )
    schema_version: Literal[1] = 1
    default_index: str | None = None
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    index_guidance: IndexGuidanceConfig = Field(default_factory=IndexGuidanceConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @property
    def computation_hash(self) -> str:
        analysis = self.analysis.model_dump()
        # The classic profile must keep the exact 0.2.1 configuration identity.
        if self.analysis.engine_profile == "classic_v4":
            analysis.pop("engine_profile", None)
        return digest(
            {
                "schema_version": self.schema_version,
                "analysis": analysis,
                "search": self.search,
                "scoring": self.scoring,
            }
        )


def large_degree_settings(settings: Settings) -> Settings:
    """Search wider for counts whose endpoints are separated by many pivots."""
    search = settings.search.model_copy(
        update={
            "max_bridge_intervals": 21,
            "beam_width": 512,
            "max_expansions": 200000,
        }
    )
    return settings.model_copy(update={"search": search})


def long_term_settings(settings: Settings) -> Settings:
    """Use the full 20-year window, wider search and subwave certification."""
    analysis = settings.analysis.model_copy(
        update={
            "timeframe": "1w",
            "history_years": 20,
            "max_analysis_bars": 5000,
            "max_anchors": 8,
            "anchor_mode": "spread",
            "top_k": 8,
        }
    )
    settings = large_degree_settings(settings.model_copy(update={"analysis": analysis}))
    search = settings.search.model_copy(update={"max_subwave_depth": 1})
    return settings.model_copy(update={"search": search})


def read_settings_values(path: Path | None) -> dict:
    try:
        values = yaml.safe_load(path.read_text(encoding="utf-8")) if path else {}
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise ValueError("configuration must be a mapping")
        return values
    except (ValueError, OSError, yaml.YAMLError) as exc:
        raise InputError(f"Invalid configuration: {exc}") from exc


def settings_from_values(values: dict) -> Settings:
    try:
        # Environment takes precedence over YAML constructor values.
        defaults = Settings(**values)
        values = values.copy()
        env = Settings()
        override = {k: env.model_dump()[k] for k in env.model_fields_set}
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(values.get(key), dict):
                fields = getattr(env, key).model_fields_set
                value = {**values[key], **{f: value[f] for f in fields}}
            values[key] = value
        return Settings(**values) if override else defaults
    except ValueError as exc:
        raise InputError(f"Invalid configuration: {exc}") from exc


def load_settings(path: Path | None = None) -> Settings:
    return settings_from_values(read_settings_values(path))


def alpaca_credentials() -> tuple[SecretStr, SecretStr]:
    from dotenv import dotenv_values

    local = dotenv_values(".env")
    key = os.environ.get("ALPACA_API_KEY", local.get("ALPACA_API_KEY", ""))
    secret = os.environ.get("ALPACA_API_SECRET", local.get("ALPACA_API_SECRET", ""))
    alias = os.environ.get("ALPACA_SECRET_KEY", local.get("ALPACA_SECRET_KEY", ""))
    if secret and alias and secret != alias:
        raise InputError("ALPACA_API_SECRET and ALPACA_SECRET_KEY conflict")
    if not key or not (secret or alias):
        raise InputError("Set ALPACA_API_KEY and ALPACA_API_SECRET in the environment or .env")
    return SecretStr(key), SecretStr(secret or alias)


def massive_api_key() -> str:
    from dotenv import dotenv_values

    local = dotenv_values(".env")
    token = os.environ.get("MASSIVE_API_KEY", local.get("MASSIVE_API_KEY", ""))
    if not token:
        raise InputError("Set MASSIVE_API_KEY in the environment or .env")
    return token


def tushare_token() -> str:
    from dotenv import dotenv_values

    local = dotenv_values(".env")
    token = os.environ.get("TUSHARE_TOKEN", local.get("TUSHARE_TOKEN", ""))
    if not token:
        raise InputError("Set TUSHARE_TOKEN in the environment or .env")
    return token


def alpaca_asset_endpoint() -> str:
    from dotenv import dotenv_values

    local = dotenv_values(".env")
    value = (
        os.environ.get("ALPACA_API_ENDPOINT", local.get("ALPACA_API_ENDPOINT"))
        or "https://paper-api.alpaca.markets"
    )
    value = value.rstrip("/")
    if value not in {"https://paper-api.alpaca.markets", "https://api.alpaca.markets"}:
        raise InputError("ALPACA_API_ENDPOINT must be an official paper or live Alpaca endpoint")
    return value
