from datetime import UTC, date, datetime, time, timedelta

import pytest

from elliottwaver.data.demo import demo_bundle
from elliottwaver.schemas.market import Bar
from elliottwaver.schemas.wave import PivotPoint


@pytest.fixture
def bundle():
    return demo_bundle()


def make_bar(i, price, *, high=None, low=None, volume=100):
    start = datetime.combine(date(2024, 1, 1) + timedelta(days=i), time(9), UTC)
    return Bar(
        start_at=start,
        end_at=start + timedelta(hours=6),
        session_date=start.date(),
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        open=price,
        close=price,
        high=high if high is not None else price,
        low=low if low is not None else price,
        volume=volume,
    )


def make_pivots(prices, *, developing=False):
    bars = [make_bar(i, price) for i, price in enumerate(prices)]
    points = tuple(
        PivotPoint(
            pivot_id=f"p{i}",
            index=i,
            extreme_bar_id=bar.bar_id,
            extreme_at=bar.start_at,
            confirmed_at=None if developing and i == len(bars) - 1 else bar.end_at,
            price=bar.close,
            kind="LOW" if i % 2 == 0 else "HIGH",
            state="DEVELOPING" if developing and i == len(bars) - 1 else "CONFIRMED",
            scale=1.4,
            threshold_at_extreme=1,
        )
        for i, bar in enumerate(bars)
    )
    return bars, points


@pytest.fixture
def approved_model():
    """Return a synthetic model with complete release metadata for registry tests."""
    from elliottwaver import ENGINE_VERSION
    from elliottwaver.config import Settings
    from elliottwaver.evaluation.features import FEATURE_VERSION, event_feature_order
    from elliottwaver.util import digest

    def build(*, name="fixture", engine=ENGINE_VERSION):
        order = event_feature_order("CLOSE_BAND_5D_V1")
        classes = ["UP", "RANGE", "DOWN"]
        model = {
            "name": name,
            "schema_version": "1.2",
            "feature_version": FEATURE_VERSION,
            "engine_version": engine,
            "config_hash": Settings().computation_hash,
            "scope": {
                "markets": ["US"],
                "timeframes": ["1d"],
                "asset_types": ["EQUITY"],
                "feed": ["synthetic"],
                "adjustment": ["synthetic_no_actions"],
            },
            "splits": {
                "train": {"start": "2023-01-01", "end": "2023-06-30"},
                "calibration": {"start": "2023-07-01", "end": "2023-12-31"},
                "test": {"start": "2024-01-01", "end": "2024-03-01"},
            },
            "protocol_sha256": "1" * 64,
            "decisions_sha256": "2" * 64,
            "events_sha256": "3" * 64,
            "release": {
                "min_test_rows": 50,
                "min_test_dates": 10,
                "min_test_symbols": 5,
                "min_bin_count": 50,
                "min_bin_dates": 10,
                "min_supported_coverage": 0.5,
                "reliability_tolerance": 0.10,
            },
            "models": {
                "CLOSE_BAND_5D_V1": {
                    "status": "APPROVED",
                    "feature_order": order,
                    "classes": classes,
                    "mean": [0.0] * len(order),
                    "scale": [1.0] * len(order),
                    "weights": [[0.0] * 3 for _ in order],
                    "intercept": [0.0] * 3,
                    "temperature": 1.0,
                    "reasons": [],
                    "test_rows": 50,
                    "test_dates": 10,
                    "test_symbols": 5,
                    "supported_coverage": 1.0,
                    "support_bins": {
                        cls: [
                            {
                                "low": 0,
                                "high": 1,
                                "count": 50,
                                "dates": 10,
                                "occupied_blocks": 2,
                                "calibration_gap_ci": [-0.02, 0.02],
                            }
                        ]
                        for cls in classes
                    },
                    "improvement": {
                        key: {"brier_ci": [0.01, 0.03], "log_loss_ci": [0.01, 0.03]}
                        for key in ("frequency", "momentum", "price")
                    },
                }
            },
        }
        model["model_id"] = digest(model)
        return model

    return build
