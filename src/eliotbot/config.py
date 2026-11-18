"""Validated configuration; credentials never enter the analysis fingerprint."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from eliotbot.errors import InputError
from eliotbot.timeframes import Timeframe, normalize_timeframe
from eliotbot.util import digest


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class AnalysisConfig(Section):
    timeframe: Timeframe = "1d"
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
    request_timeout_seconds: float = Field(20, gt=0, le=60)
    max_attempts: int = Field(3, ge=1, le=5)
    total_fetch_deadline_seconds: float = Field(120, gt=0, le=600)
    max_requests_per_second: float = Field(2, gt=0, le=8)
    delayed_access_buffer_minutes: int = Field(20, ge=15, le=120)
    cache_ttl_seconds: int = Field(21600, ge=0)


class CacheConfig(Section):
    directory: Path = Path(".cache/eliotbot")


class OutputConfig(Section):
    directory: Path = Path("reports")
    formats: tuple[Literal["html", "md", "png"], ...] = ("html", "md")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ELIOT_",
        env_nested_delimiter="__",
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )
    schema_version: Literal[1] = 1
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @property
    def computation_hash(self) -> str:
        return digest(
            {
                "schema_version": self.schema_version,
                "analysis": self.analysis,
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


def load_settings(path: Path | None = None) -> Settings:
    try:
        values = yaml.safe_load(path.read_text()) if path else {}
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise ValueError("configuration must be a mapping")
        # Environment takes precedence over YAML constructor values.
        defaults = Settings(**values)
        env = Settings()
        override = {k: env.model_dump()[k] for k in env.model_fields_set}
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(values.get(key), dict):
                fields = getattr(env, key).model_fields_set
                value = {**values[key], **{f: value[f] for f in fields}}
            values[key] = value
        return Settings(**values) if override else defaults
    except (ValueError, OSError, yaml.YAMLError) as exc:
        raise InputError(f"Invalid configuration: {exc}") from exc


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
