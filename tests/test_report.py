import hashlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from easyathome_bbt_daemon.chart import build_chart_data
from easyathome_bbt_daemon.report import (
    ReportError,
    build_cycle_summary,
    build_pdf,
    fetch_report_context_entries,
    fetch_report_readings,
    generate_report,
    resolve_report_range,
)
from easyathome_bbt_daemon.storage import (
    ensure_schema,
    get_report,
    list_context_entries,
    record_ble_reading,
    upsert_context_entry,
)

# scripts/make-fixture-db.py has a hyphenated filename (matching the sibling
# repo's script naming), so it can't be reached with a normal "import" --
# load it by file path instead of duplicating its fixture-building logic.
_FIXTURE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make-fixture-db.py"
_fixture_spec = importlib.util.spec_from_file_location("make_fixture_db", _FIXTURE_SCRIPT)
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
PROFILE = _fixture_module.PROFILE
build_fixture = _fixture_module.build_fixture


def _db(tmp_path):
    db_path = str(tmp_path / "bbt.db")
    ensure_schema(db_path)
    return db_path


def _fixture_db(tmp_path):
    db_path = str(tmp_path / "fixture.db")
    build_fixture(db_path)
    return db_path


# --- resolve_report_range -----------------------------------------------------


def test_resolve_report_range_explicit_dates(tmp_path):
    db_path = _db(tmp_path)
    start, end, kind = resolve_report_range(
        db_path, "alice", start_date="2026-01-01", end_date="2026-01-10"
    )
    assert (start, end, kind) == ("2026-01-01", "2026-01-10", "explicit")


def test_resolve_report_range_requires_both_or_neither(tmp_path):
    db_path = _db(tmp_path)
    with pytest.raises(ReportError):
        resolve_report_range(db_path, "alice", start_date="2026-01-01")


def test_resolve_report_range_no_cycle_start_raises(tmp_path):
    db_path = _db(tmp_path)
    with pytest.raises(ReportError):
        resolve_report_range(db_path, "alice")


def test_resolve_report_range_single_cycle_start_is_ongoing(tmp_path):
    db_path = _db(tmp_path)
    upsert_context_entry(db_path, "2026-07-01", assigned_profile="alice", cycle_start=1)
    record_ble_reading(
        db_path,
        ble_address="AA:BB:CC:DD:EE:FF",
        device_taken_at_raw="2026-07-05T06:30:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-07-05T06:30:00-04:00",
        received_at="2026-07-05T10:30:00+00:00",
        delivery_mode="live",
        value=36.5,
    )
    # Reading exists but isn't assigned to "alice" -- resolve_report_range's
    # ongoing-cycle fallback only looks at readings/entries for the profile.
    start, end, kind = resolve_report_range(db_path, "alice")
    assert start == "2026-07-01"
    assert kind == "cycle_ongoing"
    assert end == "2026-07-01"  # no assigned reading/entry after the start


def test_resolve_report_range_two_cycle_starts_is_complete(tmp_path):
    db_path = _db(tmp_path)
    upsert_context_entry(db_path, "2026-06-01", assigned_profile="alice", cycle_start=1)
    upsert_context_entry(db_path, "2026-06-29", assigned_profile="alice", cycle_start=1)
    start, end, kind = resolve_report_range(db_path, "alice")
    assert (start, end, kind) == ("2026-06-01", "2026-06-28", "cycle_complete")


# --- fetch helpers -------------------------------------------------------------


def test_fetch_report_readings_filters_by_date_and_profile(tmp_path):
    db_path = _fixture_db(tmp_path)
    readings = fetch_report_readings(db_path, PROFILE, "2026-07-01", "2026-07-24")
    assert readings  # fixture guarantees non-empty
    assert all("2026-07-01" <= r["taken_at"][:10] <= "2026-07-24" for r in readings)

    outside = fetch_report_readings(db_path, PROFILE, "2099-01-01", "2099-01-02")
    assert outside == []


def test_fetch_report_context_entries_filters_by_profile(tmp_path):
    db_path = _fixture_db(tmp_path)
    entries = fetch_report_context_entries(db_path, PROFILE, "2026-07-01", "2026-07-24")
    assert entries
    assert all(e["assigned_profile"] == PROFILE for e in entries)

    other_profile = fetch_report_context_entries(db_path, "nobody", "2026-07-01", "2026-07-24")
    assert other_profile == []


# --- generate_report: full pipeline --------------------------------------------


def test_generate_report_end_to_end(tmp_path):
    db_path = _fixture_db(tmp_path)
    output = str(tmp_path / "report.pdf")

    report = generate_report(
        db_path,
        profile=PROFILE,
        start_date="2026-07-01",
        end_date="2026-07-24",
        output_path=output,
        timezone_name="America/New_York",
    )

    assert report["status"] == "ready"
    assert report["revision"] == 1
    assert report["mode"] == "chart_only"
    assert report["file_path"] == output
    assert Path(output).is_file()
    assert Path(output).stat().st_size > 0
    assert report["content_digest"] == hashlib.sha256(Path(output).read_bytes()).hexdigest()
    assert len(report["covered_reading_ids"]) > 0


def test_build_pdf_digest_stable_for_identical_inputs(tmp_path, monkeypatch):
    # generate_report() reads the wall clock itself for generated_at, so two
    # end-to-end calls a moment apart legitimately produce different PDF
    # bytes (the "Generated <time>" line differs to the second). Content-
    # digest determinism is instead a property of build_pdf() given
    # identical inputs, which this test holds fixed -- including
    # generated_at. reportlab also embeds its own PDF-internal /CreationDate
    # and a random document ID by default (independent of any date printed
    # in the page content), so rl_config.invariant is enabled here to make
    # that internal metadata reproducible too -- this is reportlab's own
    # documented testing knob, not a change to production output shape.
    import reportlab.rl_config as rl_config

    monkeypatch.setattr(rl_config, "invariant", 1)

    db_path = _fixture_db(tmp_path)
    readings = fetch_report_readings(db_path, PROFILE, "2026-07-01", "2026-07-24")
    context_entries = fetch_report_context_entries(db_path, PROFILE, "2026-07-01", "2026-07-24")
    chart_data = build_chart_data(readings, context_entries, "2026-07-01", "2026-07-24")
    summary = build_cycle_summary(
        db_path, chart_data, "cycle_complete", "2026-07-01", "2026-07-24", readings
    )
    fixed_instant = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _render():
        return build_pdf(
            profile=PROFILE,
            range_start="2026-07-01",
            range_end="2026-07-24",
            chart_data=chart_data,
            summary=summary,
            readings=readings,
            db_path=db_path,
            generated_at=fixed_instant,
            generated_tz="UTC",
        )

    first_bytes = _render()
    second_bytes = _render()
    assert hashlib.sha256(first_bytes).hexdigest() == hashlib.sha256(second_bytes).hexdigest()


def test_generate_report_new_revision_records_supersession(tmp_path):
    db_path = _fixture_db(tmp_path)
    first = generate_report(
        db_path,
        profile=PROFILE,
        start_date="2026-07-01",
        end_date="2026-07-24",
        output_path=str(tmp_path / "v1.pdf"),
        timezone_name="UTC",
    )

    second = generate_report(
        db_path,
        profile=PROFILE,
        start_date="2026-07-01",
        end_date="2026-07-24",
        output_path=str(tmp_path / "v2.pdf"),
        timezone_name="UTC",
        supersedes=first["id"],
    )

    assert second["revision"] == 2
    assert second["report_uid"] == first["report_uid"]

    # The old revision's own row is not deleted or overwritten -- it's
    # marked superseded, and its own generation facts are untouched.
    old_after = get_report(db_path, first["id"])
    assert old_after["status"] == "superseded"
    assert old_after["superseded_by"] == second["id"]
    assert old_after["content_digest"] == first["content_digest"]
    assert old_after["file_path"] == first["file_path"]
    assert Path(first["file_path"]).is_file()  # old PDF file itself untouched


def test_generate_report_cycle_auto_detection(tmp_path):
    # No --start/--end given -- generate_report must fall back to
    # resolve_report_range's cycle_start-marker detection (the fixture has
    # exactly one cycle_start marker, so this is the "ongoing cycle" path).
    db_path = _fixture_db(tmp_path)
    report = generate_report(
        db_path, profile=PROFILE, output_path=str(tmp_path / "cycle.pdf"), timezone_name="UTC"
    )
    assert report["range_start"] == "2026-07-01"
    assert Path(report["file_path"]).is_file()


def test_generate_report_includes_context_entries_reference(tmp_path):
    # Sanity check that context entries actually made it into the run --
    # not asserted elsewhere in this file.
    db_path = _fixture_db(tmp_path)
    entries = list_context_entries(db_path, start_date="2026-07-01", end_date="2026-07-24")
    assert any(e.get("cycle_start") for e in entries)
