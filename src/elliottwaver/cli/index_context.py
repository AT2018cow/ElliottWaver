"""Resolve optional market inputs and already-issued daily index plans."""

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from elliottwaver.brief import BriefRun
from elliottwaver.config import Settings
from elliottwaver.data.periods import background_bundle
from elliottwaver.data.router import route_symbol
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.errors import ElliottWaverError
from elliottwaver.history import HistoryStore
from elliottwaver.index_context import build_market_context
from elliottwaver.index_intraday import ParentIssuance, build_intraday_tracking
from elliottwaver.schemas.brief import Brief
from elliottwaver.schemas.index_context import IndexContextDataset
from elliottwaver.schemas.market import AnalysisRequest, MarketDataBundle
from elliottwaver.util import atomic_write, digest
from elliottwaver.visualization.brief_formatter import read_issuance


@dataclass(frozen=True)
class ResolvedIndexContext:
    daily: MarketDataBundle | None = None
    hourly: MarketDataBundle | None = None
    reference: MarketDataBundle | None = None
    reference_symbol: str | None = None
    reference_status: str | None = None
    dataset: IndexContextDataset | None = None
    dataset_sha256: str | None = None
    dataset_status: str | None = None
    parent: Brief | None = None
    origins: tuple[tuple[str, ParentIssuance], ...] = ()

    @property
    def identity(self) -> str:
        return digest(
            [
                self.daily.snapshot_id if self.daily else None,
                self.hourly.snapshot_id if self.hourly else None,
                self.reference.snapshot_id if self.reference else None,
                self.reference_symbol,
                self.reference_status,
                self.dataset_sha256,
                self.dataset_status,
                self.parent.brief_digest if self.parent else None,
                [(key, origin.digest, origin.visible_at) for key, origin in self.origins],
            ]
        )


def _parents(
    brief: Brief,
    history: HistoryStore,
    store: SnapshotStore,
) -> tuple[Brief | None, tuple[tuple[str, ParentIssuance], ...]]:
    for row in history.query(
        brief.symbol,
        as_of=brief.market_asof,
        mode=brief.mode,
        timeframe="1d",
        limit=100,
        retrospective=brief.retrospective,
        visible_by=brief.as_of,
    ):
        try:
            parent = history.load(row)
            visible = read_issuance_from_row(row, parent)
        except (ElliottWaverError, OSError, ValueError):
            return None, ()
        if parent.retrospective != brief.retrospective or visible is None or visible > brief.as_of:
            continue
        if parent.index_guidance is None:
            return parent, ()
        origins = []
        for track in parent.index_guidance.tracks:
            plan = track.plan
            if plan is None:
                continue
            original = history.plan_origin(
                brief.symbol,
                brief.mode,
                plan.plan_id,
                plan.revision,
                plan=plan,
                retrospective=brief.retrospective,
            )
            if original is None:
                continue
            try:
                issued = history.load(original)
                receipt = read_issuance_from_row(original, issued)
                if (
                    receipt is None
                    or issued.retrospective != brief.retrospective
                    or receipt > brief.as_of
                    or issued.index_guidance is None
                    or not any(item.plan == plan for item in issued.index_guidance.tracks)
                ):
                    continue
                daily = store.load(plan.input_snapshot_id)
            except (ElliottWaverError, OSError, ValueError):
                continue
            origins.append((plan.plan_id, ParentIssuance(issued.brief_digest, receipt, daily)))
        return parent, tuple(origins)
    return None, ()


def read_issuance_from_row(row, brief: Brief) -> datetime | None:
    if brief.mode == "LIVE":
        return read_issuance(Path(row["report_path"]), brief)
    if brief.mode == "FROZEN_DATA_REPLAY":
        return brief.outlook.issued_at if brief.outlook else None
    return None


def resolve_index_context(
    run: BriefRun,
    request: AnalysisRequest,
    settings: Settings,
    history: HistoryStore,
    store: SnapshotStore,
    *,
    reference_snapshot: str | None = None,
) -> ResolvedIndexContext | None:
    policy = settings.index_guidance
    brief = run.brief
    if (
        not policy.enabled
        or run.prepared.instrument.asset_type != "INDEX"
        or brief.timeframe not in {"1d", "15m"}
        or not (
            policy.market_context
            or policy.event_context
            or (brief.timeframe == "15m" and policy.intraday_tracking)
        )
    ):
        return None

    daily = None
    if brief.timeframe == "15m" and policy.market_context:
        try:
            daily = background_bundle(run.source, "1d", as_of=brief.as_of)
            store.save(daily)
        except (ElliottWaverError, OSError):
            daily = None
    ref = None
    reference_symbol = policy.references.get(brief.symbol)
    reference_status = None
    if policy.market_context and (reference_symbol or reference_snapshot):
        try:
            if reference_symbol:
                reference_symbol = route_symbol(reference_symbol).canonical_symbol
            if reference_symbol == brief.symbol:
                reference_status = "SAME_INDEX"
            elif reference_snapshot:
                ref = store.load(reference_snapshot)
                reference_symbol = reference_symbol or ref.instrument.canonical_symbol
            elif request.mode == "LIVE":
                ref = fetch_bundle(
                    AnalysisRequest(symbol=reference_symbol, as_of=request.as_of, timeframe="1d"),
                    settings,
                )
                store.save(ref)
            else:
                reference_status = "REFERENCE_SNAPSHOT_REQUIRED"
        except (ElliottWaverError, OSError, ValueError):
            reference_status = "REFERENCE_INPUT_UNAVAILABLE"

    dataset = None
    dataset_hash = None
    dataset_status = "NOT_CONFIGURED"
    if (policy.market_context or policy.event_context) and (
        path := policy.context_files.get(brief.symbol)
    ):
        try:
            data = path.read_bytes()
            dataset_hash = sha256(data).hexdigest()
            parsed = IndexContextDataset.model_validate_json(data)
            if parsed.symbol == brief.symbol:
                atomic_write(store.root / "index-context" / f"{dataset_hash}.json", data)
                dataset = parsed
                dataset_status = "AVAILABLE"
            else:
                dataset_status = "WRONG_INDEX"
        except (OSError, ValueError):
            dataset_status = "INPUT_UNAVAILABLE"
    parent, origins = (None, ())
    hourly = None
    if brief.timeframe == "15m" and (policy.event_context or policy.intraday_tracking):
        parent, origins = _parents(brief, history, store)
    if brief.timeframe == "15m" and policy.intraday_tracking:
        try:
            hourly = background_bundle(run.prepared, "60m", as_of=brief.as_of)
            store.save(hourly)
        except (ElliottWaverError, OSError):
            hourly = None
    return ResolvedIndexContext(
        daily=daily,
        hourly=hourly,
        reference=ref,
        reference_symbol=reference_symbol,
        reference_status=reference_status,
        dataset=dataset,
        dataset_sha256=dataset_hash,
        dataset_status=dataset_status,
        parent=parent,
        origins=origins,
    )


def attach_index_context(
    run: BriefRun,
    resolved: ResolvedIndexContext | None,
    settings: Settings,
) -> BriefRun:
    brief = run.brief
    if resolved is None:
        if brief.index_context is None:
            return run
        # An exact daily decision can reuse its original guidance even when this report omits context.
        schema = (
            "1.3"
            if brief.index_guidance is not None
            else ("1.2" if settings.analysis.engine_profile == "hierarchical_v1" else "1.1")
        )
        brief = brief.model_copy(update={"schema_version": schema, "index_context": None})
        identity = digest(brief.model_dump(mode="json", exclude={"generated_at", "brief_digest"}))
        return replace(run, brief=brief.model_copy(update={"brief_digest": identity}))
    policy = settings.index_guidance
    context = build_market_context(
        brief,
        run.prepared,
        policy,
        daily=resolved.daily,
        parent=resolved.parent,
        reference=resolved.reference,
        reference_symbol=resolved.reference_symbol,
        reference_status=resolved.reference_status,
        dataset=resolved.dataset,
        dataset_sha256=resolved.dataset_sha256,
        dataset_status=resolved.dataset_status,
    )
    if brief.timeframe == "15m" and policy.intraday_tracking:
        intraday = build_intraday_tracking(
            brief,
            run.prepared,
            resolved.parent,
            dict(resolved.origins),
            policy,
            hourly=resolved.hourly,
        )
        context = context.model_copy(update={"intraday": intraday})
    brief = brief.model_copy(update={"schema_version": "1.4", "index_context": context})
    brief = brief.model_copy(
        update={
            "brief_digest": digest(
                brief.model_dump(mode="json", exclude={"generated_at", "brief_digest"})
            ),
        }
    )
    return replace(run, brief=brief)
