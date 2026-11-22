"""Offline CN-index acceptance, with a separate opt-in public-data smoke check."""

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from elliottwaver.config import DataConfig, Settings, load_settings
from elliottwaver.data.quality import missing_intervals
from elliottwaver.data.router import route_symbol
from elliottwaver.data.sina_provider import SinaProvider, _sina_worker_body
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.errors import DataError, InputError
from elliottwaver.evaluation.replay import replay
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.service import AnalysisService, prepare_bundle
from elliottwaver.util import atomic_write, canonical_json, digest

AS_OF = datetime(2024, 1, 12, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
DAYS = [date(2024, 1, day) for day in (2, 3, 4, 5, 8, 9, 10, 11, 12)]
ROWS = [
    {
        "date": day,
        "open": 100 + i,
        "high": 103 + i,
        "low": 99 + i,
        "close": 101 + i,
        "volume": 100 * (i + 1),
    }
    for i, day in enumerate(DAYS)
]


class Sink:
    def __init__(self):
        self.messages = []
        self.closed = False

    def send(self, value):
        self.messages.append(value)

    def close(self):
        self.closed = True


def worker_contracts() -> dict:
    attempts = 0
    for asset_type in ("INDEX", "EQUITY"):
        symbol = "sh000688" if asset_type == "INDEX" else "sz000001"
        for modern in (True, False):
            calls = []

            def daily(calls=calls, **kwargs):
                calls.append(kwargs)
                return (
                    pd.DataFrame([{"date": DAYS[0], "hfq_factor": 1}])
                    if kwargs.get("adjust") == "hfq-factor"
                    else pd.DataFrame(ROWS)
                )

            method = (
                ("index_zh_daily_sina" if modern else "stock_zh_index_daily")
                if asset_type == "INDEX"
                else ("stock_zh_a_hist_sina" if modern else "stock_zh_a_daily")
            )
            ak = SimpleNamespace(**{method: daily})
            quote = SimpleNamespace(
                text=f'var hq_str_{symbol}="审计品种,1,2,3";', raise_for_status=lambda: None
            )
            requests = SimpleNamespace(get=lambda *args, quote=quote, **kwargs: quote)
            sink = Sink()
            with patch.dict(sys.modules, {"akshare": ak, "requests": requests}):
                _sina_worker_body(sink, symbol, "20240102", "20240112", "1d", asset_type)
            assert sink.closed and len(sink.messages) == 1
            assert "error" not in sink.messages[0], sink.messages
            if asset_type == "INDEX":
                assert calls == [{"symbol": symbol}], calls
                assert sink.messages[0]["factors"] == []
            else:
                assert [item["adjust"] for item in calls] == ["", "hfq-factor"]
            attempts += 1
    return {"dispatch_cases": attempts, "index_factor_requests": 0}


def provider_contracts() -> dict:
    calls = []

    def fetcher(symbol, start, end, deadline, timeframe="1d", asset_type="EQUITY"):
        calls.append((symbol, timeframe, asset_type))
        return {
            "raw": ROWS,
            "factors": []
            if asset_type == "INDEX"
            else [{"date": DAYS[0], "hfq_factor": 1}, {"date": DAYS[-1], "hfq_factor": 2}],
            "name": "独立样本",
            "version": importlib.metadata.version("akshare"),
        }

    with tempfile.TemporaryDirectory(prefix="elliottwaver-cn-cache-", dir="/tmp") as temporary:
        settings = Settings(cache={"directory": temporary}, analysis={"history_years": 1})
        provider = SinaProvider(settings.data, fetcher=fetcher)
        index = fetch_bundle(
            AnalysisRequest(symbol="000001.SH", as_of=AS_OF), settings, provider=provider
        )
        stock = fetch_bundle(
            AnalysisRequest(symbol="000001", as_of=AS_OF), settings, provider=provider
        )
        assert index.instrument.asset_type == "INDEX" and stock.instrument.asset_type == "EQUITY"
        assert index.provenance.feed == "sina-index" and stock.provenance.feed == "sina"
        assert index.factors == () and index.bars == index.raw_bars
        assert index.bars[0].close == 101 and stock.bars[0].close == 50.5
        for symbol in ("000001.SH", "000001"):
            fetch_bundle(AnalysisRequest(symbol=symbol, as_of=AS_OF), settings, provider=provider)
        assert len(calls) == 2, calls
        weekly = fetch_bundle(
            AnalysisRequest(symbol="000001.SH", as_of=AS_OF, timeframe="1w"),
            settings,
            provider=provider,
        )
        assert len(calls) == 3 and calls[-1] == ("sh000001", "1d", "INDEX")
        assert [(b.open, b.high, b.low, b.close, b.volume) for b in weekly.bars] == [
            (100, 106, 99, 104, 1000),
            (104, 111, 103, 109, 3500),
        ]
        assert weekly.bars == weekly.raw_bars and len(weekly.daily_context) == 9
        partial = provider.fetch(
            route_symbol("000001.SH"),
            DAYS[0],
            date(2024, 1, 10),
            datetime(2024, 1, 10, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        partial_week = prepare_bundle(
            AnalysisRequest(symbol="000001.SH", timeframe="1w", as_of=partial.as_of), partial
        )
        assert len(partial_week.bars) == 1 and partial_week.bars[-1].session_date == date(
            2024, 1, 5
        )

    rejected = 0
    before = len(calls)
    for timeframe in ("1m", "5m", "15m", "30m", "60m"):
        try:
            provider.fetch(route_symbol("000001.SH"), DAYS[0], DAYS[-1], AS_OF, timeframe=timeframe)
        except DataError as exc:
            assert "INDEX_INTRADAY_UNSUPPORTED" in str(exc)
            rejected += 1
        else:
            raise AssertionError(timeframe)
    assert len(calls) == before

    def changed(rows):
        def fetch(*args, **kwargs):
            return {"raw": rows, "factors": [], "name": "边界样本", "version": "fixture"}

        return SinaProvider(DataConfig(), fetcher=fetch)

    try:
        changed(ROWS[:-1]).fetch(route_symbol("000001.SH"), DAYS[0], DAYS[-1], AS_OF)
    except DataError as exc:
        assert "STALE_DATA" in str(exc)
    else:
        raise AssertionError("Missing final session was accepted")
    missing = changed(ROWS[:1] + ROWS[2:]).fetch(
        route_symbol("000001.SH"), DAYS[0], DAYS[-1], AS_OF
    )
    assert missing_intervals(missing) == (date(2024, 1, 3),)
    assert len(missing.bars) == 8
    future = {**ROWS[-1], "date": date(2024, 1, 15), "close": float("nan")}
    bounded = changed(ROWS + [future]).fetch(route_symbol("000001.SH"), DAYS[0], DAYS[-1], AS_OF)
    assert len(bounded.bars) == 9 and bounded.bars[-1].session_date == DAYS[-1]
    try:
        prepare_bundle(AnalysisRequest(symbol="000001", as_of=AS_OF), bounded)
    except InputError:
        pass
    else:
        raise AssertionError("Stock request accepted an index snapshot")
    return {
        "shared_digits_cache_isolated": True,
        "weekly_ohlcv_oracle": True,
        "partial_week_excluded": True,
        "intraday_rejected_before_io": rejected,
        "missing_tail_rejected": True,
        "internal_gap_preserved": True,
        "future_rows_excluded": True,
        "foreign_snapshot_rejected": True,
    }


def frozen_contracts(cache_root: Path) -> dict:
    submitted = json.loads(Path("docs/validation_cn_index_20261117.json").read_text())
    store, service = SnapshotStore(cache_root), AnalysisService(load_settings())
    cases, prefixes = [], 0
    for case in submitted["cases"]:
        bundle = store.load(case["source_snapshot_id"])
        request = AnalysisRequest(
            symbol=case["symbol"],
            timeframe=case["timeframe"],
            as_of=bundle.as_of,
            mode="FROZEN_DATA_REPLAY",
        )
        prepared = prepare_bundle(request, bundle)
        result = service.analyze_bundle(request, bundle)
        assert result.result_digest == case["result_digest"]
        assert prepared.snapshot_id == case["analyzed_snapshot_id"]
        expected = "EQUITY" if case["label"] == "pingan_day" else "INDEX"
        assert result.instrument.asset_type == expected
        if expected == "INDEX":
            assert prepared.provenance.adjustment == "none" and not prepared.factors
            assert prepared.bars == prepared.raw_bars
        manifest = json.loads((Path(case["report"]) / "manifest.json").read_text())
        recorded = json.loads((Path(case["report"]) / "analysis.json").read_text())
        assert manifest["input_snapshot_id"] == prepared.snapshot_id
        assert manifest["result_digest"] == result.result_digest
        assert recorded["input_snapshot_id"] == prepared.snapshot_id
        assert recorded["result_digest"] == result.result_digest
        assert (
            digest(
                {
                    name: value
                    for name, value in recorded.items()
                    if name not in {"run_id", "result_digest", "analyzed_at", "mode"}
                }
            )
            == result.result_digest
        )
        assert {"analysis.json", "report.md", "chart.html"} <= set(manifest["files"])
        for name, checksum in manifest["files"].items():
            assert (
                hashlib.sha256((Path(case["report"]) / name).read_bytes()).hexdigest() == checksum
            )
        for historical in replay(prepared, service, start=prepared.bars[-3].session_date):
            direct = service.analyze_bundle(
                AnalysisRequest(
                    symbol=case["symbol"],
                    timeframe=case["timeframe"],
                    as_of=historical.as_of,
                    end=historical.market_asof,
                ),
                prepared,
            )
            assert direct.result_digest == historical.result_digest
            prefixes += 1
        cases.append(
            {
                "label": case["label"],
                "snapshot_id": prepared.snapshot_id,
                "status": result.status,
                "bars": len(prepared.bars),
            }
        )
    legacy = SnapshotStore(Path(".cache/live-smoke")).load(
        "1b89533b62b28eacc88ed5f95c7e4da2478679b2b7bb5a247d6ee111d5fee124"
    )
    assert legacy.instrument.asset_type == "EQUITY"
    assert legacy.snapshot_id == "1b89533b62b28eacc88ed5f95c7e4da2478679b2b7bb5a247d6ee111d5fee124"
    return {"cases": cases, "prefixes": prefixes, "legacy_stock_snapshot_unchanged": True}


def live_smoke() -> dict:
    cases = []
    store = SnapshotStore(Path(".cache/review-cn-index-live"))
    for symbol in ("000688.SH", "399001.SZ", "899050.BJ"):
        instrument = route_symbol(symbol)
        as_of = datetime(2026, 11, 16, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        bundle = SinaProvider(DataConfig(total_fetch_deadline_seconds=45)).fetch(
            instrument, date(2026, 7, 1), date(2026, 11, 16), as_of
        )
        assert bundle.instrument.identity_verified and bundle.instrument.asset_type == "INDEX"
        assert bundle.provenance.feed == "sina-index" and bundle.provenance.adjustment == "none"
        assert not bundle.factors and bundle.bars == bundle.raw_bars
        assert bundle.bars[-1].session_date == date(2026, 11, 16)
        result = AnalysisService().analyze_bundle(
            AnalysisRequest(symbol=symbol, as_of=as_of), bundle
        )
        assert result.status not in {"STALE_DATA", "INSUFFICIENT_DATA"}
        identity = store.save(bundle)
        item = {
            "symbol": symbol,
            "name": bundle.instrument.name,
            "bars": len(bundle.bars),
            "snapshot_id": identity,
            "last_session": bundle.bars[-1].session_date,
            "status": result.status,
        }
        cases.append(item)
        print(canonical_json(item), flush=True)
    return {"cases": cases, "cache_root": str(store.root)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--cache-root", type=Path, default=Path(".cache/elliottwaver"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    checks = (
        {"live_public_sina": live_smoke()}
        if args.live
        else {
            "worker_dispatch": worker_contracts(),
            "provider_boundaries": provider_contracts(),
            "frozen_regression": frozen_contracts(args.cache_root),
        }
    )
    source_hash = digest(
        {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path("src").rglob("*.py"))
        }
    )
    if not args.live:
        assert (
            source_hash
            == json.loads(Path("docs/validation_cn_index_20261117.json").read_text())[
                "source_sha256"
            ]
        )
    evidence = {
        "reviewed_commit": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
        "reviewed_at": datetime.now(UTC),
        "source_sha256": source_hash,
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "mode": "LIVE_PUBLIC_READONLY" if args.live else "OFFLINE",
        "status": "PASS",
        "checks": checks,
        "scope": "CN index integration and stock regression; not PIT, calibrated probabilities or profitability",
    }
    output = args.output or Path(
        "docs/reviews/2026-11-17-cn-index-live-evidence.json"
        if args.live
        else "docs/reviews/2026-11-17-cn-index-evidence.json"
    )
    atomic_write(output, canonical_json(evidence) + "\n")
    print(f"PASS {evidence['mode']}: {output}")


if __name__ == "__main__":
    main()
