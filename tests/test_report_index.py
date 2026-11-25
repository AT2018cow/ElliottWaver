from datetime import datetime, timedelta
from shutil import copytree

import elliottwaver.brief as brief_module
from elliottwaver.brief import build_brief
from elliottwaver.config import Settings
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.visualization.brief_formatter import publish_brief
from elliottwaver.visualization.report_index import (
    ISSUANCE_FILE,
    ISSUANCE_PENDING,
    write_report_index,
)


def test_report_index_prefers_verified_live_archive_and_falls_back(bundle, tmp_path, monkeypatch):
    bundle = bundle.model_copy(
        update={"provenance": bundle.provenance.model_copy(update={"feed": "sip"})}
    )
    now = bundle.as_of

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(brief_module, "datetime", Clock)
    symbol_dir = tmp_path / bundle.instrument.canonical_symbol

    def archive(mode, as_of, *, end=None, formats=("md",)):
        nonlocal now
        now = as_of
        request = AnalysisRequest(
            symbol=bundle.instrument.canonical_symbol, as_of=as_of, end=end, mode=mode
        )
        source = bundle.model_copy(update={"as_of": as_of})
        run = build_brief(request, source, Settings(), include_weekly=False, check_windows=False)
        return publish_brief(run, tmp_path, formats=formats, clock=lambda: now)

    current = archive("LIVE", bundle.as_of)
    earlier_day = bundle.bars[-2].session_date
    backfill = archive("LIVE", bundle.as_of + timedelta(minutes=1), end=earlier_day)
    frozen = archive("FROZEN_DATA_REPLAY", bundle.as_of + timedelta(minutes=2), formats=())
    pending = copytree(current, current.with_name(current.name + "_pending"))
    (pending / ISSUANCE_PENDING).touch()

    def page():
        return write_report_index(symbol_dir).read_text()

    def link(path, filename="report.md"):
        return (path / filename).relative_to(symbol_dir).as_posix()

    visible = page()
    live_section, other_sections = visible.split("<h2>回放和演示</h2>")
    assert link(current) in live_section
    assert link(backfill) not in live_section
    assert link(frozen, "brief.json") in other_sections
    assert link(backfill) in other_sections
    assert link(pending) not in visible

    receipt = current / ISSUANCE_FILE
    original = receipt.read_bytes()
    receipt.write_text("{}")
    visible = page()
    assert link(backfill) in visible.split("<h2>回放和演示</h2>")[0]
    assert link(current) not in visible

    receipt.write_bytes(original)
    (current / "report.md").write_text("tampered linked file")
    visible = page()
    assert link(backfill) in visible.split("<h2>回放和演示</h2>")[0]
    assert link(current) not in visible
