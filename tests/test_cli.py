import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests
import yaml
from typer.testing import CliRunner

from elliottwaver import __version__
from elliottwaver import brief as brief_module
from elliottwaver.cli import main as cli
from elliottwaver.cli.daily import intraday_availability
from elliottwaver.cli.main import app
from elliottwaver.config import Settings, load_settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.router import route_symbol
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.errors import DataError, SourceError
from elliottwaver.history import HistoryStore
from elliottwaver.visualization import brief_formatter


def test_version_exits_without_a_subcommand():
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"{__version__}\n"


def test_doctor_lists_all_supported_pattern_families():
    result = CliRunner().invoke(app, ["doctor", "--market", "CN"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["supported_patterns"] == [
        "IMPULSE",
        "ZIGZAG_ABC",
        "TRIANGLE",
        "FLAT",
        "DIAGONAL",
        "WXY",
    ]


def test_daily_commands_are_discoverable_without_advanced_options():
    runner = CliRunner()
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0, root.output
    assert "elliottwaver 000688.SH" in root.output
    assert all(name in root.output for name in ("brief", "intraday", "history"))
    short = runner.invoke(app, ["brief", "--help"])
    full = runner.invoke(app, ["brief", "--help-all"])
    assert short.exit_code == full.exit_code == 0
    assert "--details" in short.output and "--snapshot" not in short.output
    assert "--snapshot" in full.output and "--model-registry" in full.output


def test_init_preserves_existing_settings_and_canonicalizes_the_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base = "analysis:\n  top_k: 5\ndata:\n  cn_index_intraday_feed: tushare-cn-index\n"
    Path("config.yaml").write_text(base)
    local = Path("config.local.yaml")
    runner = CliRunner()
    missing = runner.invoke(app, ["brief"])
    assert missing.exit_code == 2 and "elliottwaver init" in missing.stderr
    rejected = runner.invoke(app, ["init", "000688", "--yes"])
    assert rejected.exit_code == 2 and not local.exists()
    declined = runner.invoke(app, ["init", "SH000688"], input="n\n")
    assert declined.exit_code == 0 and not local.exists()
    accepted = runner.invoke(app, ["init", "SH000688"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert "科创50" in accepted.stdout and "Tushare" in accepted.stdout
    assert Path("config.yaml").read_text() == base
    settings = load_settings(local)
    assert settings.default_index == "000688.SH"
    assert settings.analysis.top_k == 5
    assert settings.data.cn_index_intraday_feed == "tushare-cn-index"
    assert settings.computation_hash == Settings(analysis={"top_k": 5}).computation_hash
    doctor = runner.invoke(app, ["doctor"])
    assert doctor.exit_code == 0, doctor.output
    assert json.loads(doctor.stdout)["market"] == "CN"
    assert json.loads(doctor.stdout)["cn_index_intraday_feed"] == "tushare-cn-index"
    changed = runner.invoke(app, ["init", "SPX", "--yes"])
    assert changed.exit_code == 0, changed.output
    values = yaml.safe_load(local.read_text())
    assert values["default_index"] == "SPX"
    assert values["data"] == {
        "cn_index_intraday_feed": "tushare-cn-index",
        "us_index_feed": "yahoo-index",
    }


def test_daily_cli_runs_real_reports_with_mocked_market_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    setup = runner.invoke(app, ["init", "SPX", "--yes"])
    assert setup.exit_code == 0, setup.output
    now = None

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    def fetch(request, settings, **kwargs):
        assert settings.data.us_index_feed == "yahoo-index"
        source = demo_bundle(request.symbol, timeframe=request.timeframe)
        bars = tuple(bar.model_copy(update={"observed_at": bar.usable_at}) for bar in source.bars)
        assert bars[-1].usable_at < now
        return source.model_copy(update={"bars": bars, "raw_bars": bars, "as_of": now})

    monkeypatch.setattr(brief_module, "datetime", Clock)
    monkeypatch.setattr(cli, "parse_as_of", lambda *args: now)
    monkeypatch.setattr(cli, "fetch_bundle", fetch)
    monkeypatch.setattr(
        cli, "publish_brief", lambda *args: brief_formatter.publish_brief(*args, clock=lambda: now)
    )
    no_history = runner.invoke(app, ["history"])
    assert no_history.exit_code == 0 and "elliottwaver brief" in no_history.stdout

    now = demo_bundle("SPX", timeframe="15m").as_of + timedelta(minutes=1)
    intraday = runner.invoke(app, ["intraday"])
    assert intraday.exit_code == 0, intraday.output
    assert "60 分钟背景" in intraday.stdout and "暂无已发行日线预案" in intraday.stdout
    assert "先运行 elliottwaver brief SPX" in intraday.stdout

    now = demo_bundle("SPX").as_of + timedelta(minutes=1)
    daily = runner.invoke(app, ["brief"])
    assert daily.exit_code == 0, daily.output
    assert "短线 5 个交易日" in daily.stdout and "波段 20 个交易日" in daily.stdout
    assert "核对依据" not in daily.stdout
    details = runner.invoke(app, ["brief", "--details"])
    assert details.exit_code == 0, details.output
    assert "核对依据" in details.stdout
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    chart = runner.invoke(app, ["brief", "--open"])
    assert chart.exit_code == 0, chart.output
    daily_chart = next(Path("reports/SPX/archive/brief").glob(f"{now.date()}_*/index.html"))
    assert opened == [daily_chart.resolve().as_uri()]

    temporary = runner.invoke(app, ["brief", "NDX", "--json"])
    assert temporary.exit_code == 0, temporary.output
    assert json.loads(temporary.stdout)["symbol"] == "NDX"
    history = runner.invoke(app, ["history", "--json"])
    assert history.exit_code == 0, history.output
    records = json.loads(history.stdout)
    assert [row["timeframe"] for row in records] == ["1d", "15m"]
    assert all(row["mode"] == "LIVE" and Path(row["path"]).is_dir() for row in records)
    terminal_history = runner.invoke(app, ["history"])
    assert terminal_history.exit_code == 0, terminal_history.output
    assert "报告入口" in terminal_history.stdout and "SPX/index.html" in terminal_history.stdout


def test_intraday_direct_command_uses_index_default_without_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def fetch(request, settings, **kwargs):
        assert request.symbol == "000688.SH" and request.timeframe == "15m"
        assert settings.data.cn_index_intraday_feed == "tencent-cn-index"
        raise DataError("测试行情不可用")

    monkeypatch.setattr(cli, "fetch_bundle", fetch)
    response = CliRunner().invoke(app, ["intraday", "000688.SH"])
    assert response.exit_code == 3
    assert "测试行情不可用" in response.stderr


@pytest.mark.parametrize(
    ("symbol", "local_time", "expected"),
    [
        ("000688.SH", "2024-06-05T09:29:59", "CLOSED"),
        ("000688.SH", "2024-06-05T09:44:59", "AWAITING_BAR"),
        ("000688.SH", "2024-06-05T09:45:00", "AVAILABLE"),
        ("000688.SH", "2024-06-05T11:30:00", "CLOSED"),
        ("000688.SH", "2024-06-05T13:00:00", "AVAILABLE"),
        ("000688.SH", "2024-06-05T15:00:00", "CLOSED"),
        ("000688.SH", "2024-06-08T10:30:00", "CLOSED"),
        ("000688.SH", "2024-10-01T10:30:00", "CLOSED"),
        ("SPX", "2024-11-28T10:30:00", "CLOSED"),
        ("SPX", "2024-11-29T10:04:59", "AWAITING_BAR"),
        ("SPX", "2024-11-29T10:05:00", "AVAILABLE"),
        ("SPX", "2024-11-29T13:00:00", "CLOSED"),
        ("SPX", "2024-07-05T13:45:00+00:00", "AWAITING_BAR"),
    ],
)
def test_symbol_mode_respects_market_hours_and_bar_availability(symbol, local_time, expected):
    instrument = route_symbol(symbol)
    now = datetime.fromisoformat(local_time)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(instrument.timezone))
    assert intraday_availability(instrument, now.astimezone(UTC), 20) == expected


def test_symbol_only_runs_periods_and_shows_previous_reports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config.yaml").write_text("output:\n  formats: [html, png]\n")
    runner = CliRunner()
    zone = ZoneInfo("America/New_York")
    now = datetime(2025, 1, 31, 9, 20, tzinfo=zone)
    calls = []
    fail_minutes = False

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    def fetch(request, settings, **kwargs):
        calls.append((request.symbol, request.timeframe))
        if request.symbol == "SPX":
            assert settings.data.us_index_feed == "yahoo-index"
        assert request.mode == "LIVE"
        source = demo_bundle(request.symbol, timeframe=request.timeframe)
        minutes = request.timeframe == "15m"
        if minutes and fail_minutes:
            raise SourceError("尚未收到最新分时行情")
        shift = timedelta(days=381 if minutes else 0)
        bars = []
        for bar in source.bars:
            if bar.usable_at + shift > now:
                continue
            bars.append(
                bar.model_copy(
                    update={
                        "start_at": bar.start_at + shift,
                        "end_at": bar.end_at + shift,
                        "available_at": bar.usable_at + shift,
                        "observed_at": bar.usable_at + shift,
                        "session_date": bar.session_date + shift,
                    }
                )
            )
        return source.model_copy(
            update={
                "bars": tuple(bars),
                "raw_bars": tuple(bars),
                "as_of": request.as_of,
                "sessions": tuple(day + shift for day in source.sessions),
                "expected_bar_starts": tuple(stamp + shift for stamp in source.expected_bar_starts),
                "provenance": source.provenance.model_copy(
                    update={"basis_date": bars[-1].session_date}
                ),
            }
        )

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(brief_module, "datetime", Clock)
    monkeypatch.setattr(cli, "fetch_bundle", fetch)
    monkeypatch.setattr(
        cli, "publish_brief", lambda *args: brief_formatter.publish_brief(*args, clock=lambda: now)
    )
    premarket = runner.invoke(app, ["SPX"])
    assert premarket.exit_code == 0, premarket.output
    assert calls == [("SPX", "1d")]
    assert "当前为非交易时段" in premarket.stdout and "与上次相比" not in premarket.stdout
    assert not Path("config.local.yaml").exists()

    now = datetime(2025, 1, 31, 9, 55, tzinfo=zone)
    pending = runner.invoke(app, ["SPX"])
    assert pending.exit_code == 0, pending.output
    assert calls == [("SPX", "1d")] * 2
    assert "盘中行情待更新" in pending.stdout and "与上次相比" in pending.stdout

    now = datetime(2025, 1, 31, 15, 18, tzinfo=zone)
    open_market = runner.invoke(app, ["SPX"])
    assert open_market.exit_code == 0, open_market.output
    assert calls[-2:] == [("SPX", "1d"), ("SPX", "15m")]
    assert "60 分钟背景" in open_market.stdout
    assert open_market.stdout.count("与上次相比") == 1
    assert "先运行 elliottwaver brief" not in open_market.stdout
    assert open_market.stdout.count("报告入口 · reports/SPX/index.html") == 1
    index = Path("reports/SPX/index.html").read_text()
    assert "日线简报" in index and "15 分钟线简报" in index
    assert "archive/brief/" in index
    assert list(Path("reports/SPX/archive/brief").rglob("index.html"))
    assert not list(Path("reports/SPX/archive/brief").rglob("*.png"))
    rows = HistoryStore(Path(".cache/elliottwaver")).query("SPX", mode="LIVE")
    assert {row["timeframe"] for row in rows} == {"1d", "15m"}

    equity = runner.invoke(app, ["AAPL"])
    assert equity.exit_code == 0, equity.output
    assert calls[-2:] == [("AAPL", "1d"), ("AAPL", "15m")]
    assert "分析价格" in equity.stdout and "60 分钟背景" in equity.stdout
    assert "日线预案" not in equity.stdout
    manual = runner.invoke(app, ["intraday", "AAPL", "--json"])
    assert manual.exit_code == 0, manual.output
    assert json.loads(manual.stdout)["timeframe"] == "15m"

    fail_minutes = True
    now += timedelta(minutes=1)
    partial = runner.invoke(app, ["SPX"])
    assert partial.exit_code == 0, partial.output
    assert "SPX · 日线" in partial.stdout
    assert "尚未收到最新分时行情" in partial.stdout
    assert "报告入口 · reports/SPX/index.html" in partial.stdout


def test_init_settings_apply_to_fetch_inspect_and_end_bounded_analysis(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    missing = runner.invoke(app, ["data", "fetch", "SPX"])
    assert missing.exit_code == 3
    assert "elliottwaver init SPX" in missing.stderr

    Path("config.yaml").write_text("cache:\n  directory: custom-cache\n")
    result = runner.invoke(app, ["init", "SPX", "--yes"])
    assert result.exit_code == 0, result.output
    bundle = demo_bundle("SPX")
    bundle = bundle.model_copy(
        update={"provenance": bundle.provenance.model_copy(update={"feed": "yahoo-index"})}
    )

    def fetch(request, settings, **kwargs):
        assert settings.data.us_index_feed == "yahoo-index"
        assert settings.cache.directory == Path("custom-cache")
        source = bundle.model_copy(update={"as_of": request.as_of})
        SnapshotStore(settings.cache.directory).save(source)
        return source

    monkeypatch.setattr(cli, "fetch_bundle", fetch)
    fetched = runner.invoke(app, ["data", "fetch", "SPX"])
    assert fetched.exit_code == 0, fetched.output
    snapshot = json.loads(fetched.stdout)["snapshot_id"]
    inspected = runner.invoke(app, ["data", "inspect", snapshot])
    assert inspected.exit_code == 0, inspected.output

    end = bundle.bars[-2].session_date
    historical = runner.invoke(
        app, ["analyze", "SPX", "--end", str(end), "--format", "json", "--json"]
    )
    assert historical.exit_code == 0, historical.output
    report = json.loads(historical.stdout)
    assert report["mode"] == "FROZEN_DATA_REPLAY" and report["market_asof"] == str(end)
    index = Path("reports/SPX/index.html").read_text()
    assert "回放和演示" in index and "最新实时报告" not in index


@pytest.mark.parametrize(
    ("token", "message"), [("", "TUSHARE_AUTH_REQUIRED"), ("fixture", "TUSHARE_HTTP")]
)
def test_tushare_failure_keeps_daily_report_entry(tmp_path, monkeypatch, token, message):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TUSHARE_TOKEN", token)
    Path("config.yaml").write_text(
        "data:\n  cn_index_intraday_feed: tushare-cn-index\noutput:\n  formats: []\n"
    )
    now = datetime(2024, 9, 19, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    bundle = demo_bundle("000688.SH")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    def fetch(request, settings, **kwargs):
        if request.timeframe == "15m":
            return fetch_bundle(request, settings, **kwargs)
        return bundle.model_copy(update={"as_of": request.as_of})

    def timeout(*args, **kwargs):
        raise requests.Timeout("transport diagnostics")

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(brief_module, "datetime", Clock)
    monkeypatch.setattr(cli, "fetch_bundle", fetch)
    monkeypatch.setattr(requests, "post", timeout)
    monkeypatch.setattr(
        cli, "publish_brief", lambda *args: brief_formatter.publish_brief(*args, clock=lambda: now)
    )
    result = CliRunner().invoke(app, ["000688.SH"])
    assert result.exit_code == 0, result.output
    assert "000688.SH · 日线" in result.stdout
    assert message in result.stdout
    assert "报告入口 · reports/000688.SH/index.html" in result.stdout
