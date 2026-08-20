"""Immutable PDF report generation (addendum section 9).

Chart-only only, this phase -- ``generation_params["mode"]`` is always
``"chart_only"``; there is no interpretation engine to select one of
Sensiplan/SymptoPro/TCOYF from (addendum 7). Report generation is a CLI
tool (``easyathome-bbt-report``), run by hand or from cron, mirroring how
the sibling ``health-thermometer-daemon`` generates its PDFs -- there is no
HTTP endpoint that triggers generation in this phase (addendum section 10's
Hub-facing report-initiation API is a later phase).

This module deliberately reuses ``chart.build_chart_data`` as the single
source of per-day data for *both* the rendered chart and the daily detail
table/summary below it, rather than querying twice -- addendum 9.3 requires
"dashboard and PDF projections must use the same reporting/query layer so
their results do not disagree," and sharing one ``ChartData.days`` list is
how that's enforced here, not just documented.

Reports are immutable revisions (addendum 9.3): a new call to
``generate_report`` never edits a previous PDF or its recorded row. Passing
``supersedes=<old report id>`` records a new revision linked to the old one
via ``storage.record_report``/``storage.supersede_report`` -- the old row's
own generation facts (its digest, its file, its timestamp) are never
touched.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ._version import __version__
from .chart import ChartData, DayPoint, build_chart_data, render_chart
from .config import ConfigError, load_config
from .storage import (
    StorageError,
    ensure_schema,
    get_device,
    get_reading_corrections,
    get_reading_exclusion_history,
    get_reading_time_corrections,
    get_report,
    list_context_entries,
    list_readings,
    record_report,
    supersede_report,
    update_report_status,
)

_TABLE_STYLE_COMMANDS = [
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f5d8a")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 7),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
]


class ReportError(Exception):
    """Raised for report-generation misuse, e.g. an unresolvable date range."""


def resolve_report_range(
    db_path: str, profile: str, *, start_date: str | None = None, end_date: str | None = None
) -> tuple[str, str, str]:
    """Resolve the reporting period: explicit dates, or a cycle_start-bounded cycle.

    Per addendum 7.1, "cycle" here means only the profile's own
    ``context_entries.cycle_start = 1`` markers -- no interpretation engine
    is consulted, since chart-only mode doesn't have one.

    Args:
        db_path: Filesystem path to the SQLite database file.
        profile: The profile (``assigned_profile``) to resolve a cycle for.
        start_date: Explicit inclusive start date (``YYYY-MM-DD``), if given.
        end_date: Explicit inclusive end date (``YYYY-MM-DD``), if given.

    Returns:
        ``(range_start, range_end, range_kind)`` where ``range_kind`` is
        ``"explicit"`` (caller-supplied dates), ``"cycle_complete"`` (bounded
        by two cycle_start markers -- the addendum's "two context_entries
        with cycle_start=1 bounding it, when available"), or
        ``"cycle_ongoing"`` (bounded only by the latest cycle_start marker,
        extended through however much data exists since -- used when only
        one boundary marker exists yet).

    Raises:
        ReportError: If exactly one of ``start_date``/``end_date`` is given,
            or neither was given and no cycle_start marker exists for this
            profile.
    """
    if start_date and end_date:
        return start_date, end_date, "explicit"
    if bool(start_date) != bool(end_date):
        raise ReportError("--start and --end must both be given, or neither")

    starts = sorted(
        str(entry["entry_date"])
        for entry in list_context_entries(db_path)
        if entry.get("assigned_profile") == profile and entry.get("cycle_start")
    )
    if not starts:
        raise ReportError(
            f"No cycle_start context entry found for profile {profile!r}; "
            "pass --start/--end explicitly"
        )

    if len(starts) >= 2:
        range_start, next_boundary = starts[-2], starts[-1]
        range_end = (date.fromisoformat(next_boundary) - timedelta(days=1)).isoformat()
        return range_start, range_end, "cycle_complete"

    range_start = starts[-1]
    candidate_dates = [
        str(reading["taken_at"])[:10]
        for reading in list_readings(db_path, assigned_profile=profile)
    ] + [
        str(entry["entry_date"])
        for entry in list_context_entries(db_path)
        if entry.get("assigned_profile") == profile
    ]
    later_or_equal = [d for d in candidate_dates if d >= range_start]
    range_end = max(later_or_equal) if later_or_equal else range_start
    return range_start, range_end, "cycle_ongoing"


def _date_of(taken_at: str) -> str:
    """Return the calendar-date portion of an ISO ``taken_at`` timestamp."""
    return datetime.fromisoformat(taken_at).date().isoformat()


def fetch_report_readings(
    db_path: str, profile: str, start_date: str, end_date: str
) -> list[dict[str, object]]:
    """Return readings assigned to ``profile`` whose taken_at date falls in range.

    Includes excluded readings -- addendum 6/13 require reports to show
    them, never hide them. ``list_readings`` has no server-side date filter
    (its counterpart for context entries does), so the range is applied
    here in Python against each reading's resolved ``taken_at`` date.
    """
    readings = list_readings(db_path, assigned_profile=profile, include_excluded=True)
    return [r for r in readings if start_date <= _date_of(str(r["taken_at"])) <= end_date]


def fetch_report_context_entries(
    db_path: str, profile: str, start_date: str, end_date: str
) -> list[dict[str, object]]:
    """Return context entries for ``profile`` within the date range."""
    entries = list_context_entries(db_path, start_date=start_date, end_date=end_date)
    return [e for e in entries if e.get("assigned_profile") == profile]


@dataclass
class CycleSummary:
    """Concise cycle summary shown on report page 1 (addendum 9.2)."""

    range_start: str
    range_end: str
    range_kind: str
    total_days: int
    covered_days: int
    missing_days: int
    disturbance_days: int
    excluded_days: int
    lh_positive_dates: list[str]
    devices: list[str]
    manual_reading_count: int


def build_cycle_summary(
    db_path: str,
    chart_data: ChartData,
    range_kind: str,
    range_start: str,
    range_end: str,
    readings: list[dict[str, object]],
) -> CycleSummary:
    """Derive the page-1 cycle summary from the same per-day data the chart uses."""
    days = chart_data.days
    covered = sum(1 for day in days if day.value is not None)
    lh_positive = [
        day.date for day in days
        if day.lh_test_result and "positive" in day.lh_test_result.lower()
    ]

    device_ids = {int(r["device_id"]) for r in readings if r.get("device_id") is not None}
    devices = []
    for device_id in sorted(device_ids):
        device = get_device(db_path, device_id)
        if device:
            devices.append(str(device.get("label") or device["ble_address"]))
    manual_count = sum(1 for r in readings if r.get("source_type") == "manual")

    return CycleSummary(
        range_start=range_start,
        range_end=range_end,
        range_kind=range_kind,
        total_days=len(days),
        covered_days=covered,
        missing_days=len(days) - covered,
        disturbance_days=sum(1 for day in days if day.is_disturbed),
        excluded_days=sum(1 for day in days if day.is_excluded),
        lh_positive_dates=lh_positive,
        devices=devices,
        manual_reading_count=manual_count,
    )


def _summary_text(summary: CycleSummary) -> str:
    """Render the cycle summary as one summary paragraph's HTML-ish body text."""
    parts = [f"Period: {summary.range_start} to {summary.range_end}"]
    if summary.range_kind == "cycle_complete":
        parts.append(f"Cycle length: {summary.total_days} days (both boundaries known)")
    elif summary.range_kind == "cycle_ongoing":
        parts.append(
            f"Cycle in progress: {summary.total_days} day(s) so far, no next "
            "cycle_start recorded yet"
        )
    parts.append(
        f"Coverage: {summary.covered_days}/{summary.total_days} days "
        f"({summary.missing_days} missing)"
    )
    parts.append(f"Disturbed days: {summary.disturbance_days}")
    parts.append(f"Excluded days: {summary.excluded_days}")
    parts.append(
        "LH-positive day(s): " + (", ".join(summary.lh_positive_dates) or "none recorded")
    )
    return " &middot; ".join(parts)


def _context_highlights(day: DayPoint) -> str:
    """Render one day's non-empty context fields as a short summary string."""
    parts = []
    if day.menstrual_flow:
        parts.append(f"Flow: {day.menstrual_flow}")
    if day.cervical_mucus:
        parts.append(f"Mucus: {day.cervical_mucus}")
    if day.lh_test_result:
        parts.append(f"LH: {day.lh_test_result}")
    if day.disturbance_flags:
        parts.append("Disturbed: " + ", ".join(day.disturbance_flags))
    return "; ".join(parts) if parts else "-"


def _build_daily_table(
    days: list[DayPoint], readings_by_id: dict[int, dict[str, object]]
) -> Table:
    """Build the daily detail table (addendum 9.2's "subsequent pages" content)."""
    header = ["Date", "Cycle day", "Value", "Unit", "Source", "Corrected", "Excluded", "Context"]
    data: list[list[object]] = [header]
    for day in days:
        reading = readings_by_id.get(day.reading_id) if day.reading_id is not None else None
        cycle_day = str(day.cycle_day) if day.cycle_day is not None else "-"
        if reading is None:
            data.append([day.date, cycle_day, "-", "-", "-", "-", "-", _context_highlights(day)])
            continue

        corrected = reading.get("corrected_value")
        data.append(
            [
                day.date,
                cycle_day,
                f"{day.value:.2f}" if day.value is not None else "-",
                day.unit or "-",
                "BLE" if reading.get("source_type") == "ble_import" else "Manual",
                f"{corrected:.2f}" if corrected is not None else "-",
                "Y" if day.is_excluded else "N",
                _context_highlights(day),
            ]
        )

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle(_TABLE_STYLE_COMMANDS))
    return table


def _build_audit_section(db_path: str, days: list[DayPoint], styles) -> list:
    """Build the correction/exclusion summary, pulled from the audit tables.

    Addendum 9.3 explicitly requires this to come from the retained audit
    tables (``reading_corrections``/``reading_time_corrections``/
    ``reading_exclusions``), not just the cached current-value columns, so
    a report stays reproducible/explainable even after later corrections.
    """
    elements: list = [Paragraph("Correction and exclusion summary", styles["Heading2"])]
    any_events = False
    for day in days:
        if day.reading_id is None:
            continue
        corrections = get_reading_corrections(db_path, day.reading_id)
        time_corrections = get_reading_time_corrections(db_path, day.reading_id)
        exclusions = get_reading_exclusion_history(db_path, day.reading_id)
        if not (corrections or time_corrections or exclusions):
            continue

        any_events = True
        elements.append(
            Paragraph(f"<b>{escape(day.date)}</b> (reading id {day.reading_id})", styles["Normal"])
        )
        for correction in corrections:
            elements.append(
                Paragraph(
                    f"Value corrected {correction['previous_value']} "
                    f"{correction['previous_unit']} -&gt; {correction['new_value']} "
                    f"{correction['new_unit']} by {escape(str(correction['actor']))} at "
                    f"{correction['corrected_at']}: {escape(str(correction['reason']))}",
                    styles["Normal"],
                )
            )
        for time_correction in time_corrections:
            elements.append(
                Paragraph(
                    f"Time corrected {time_correction['previous_taken_at']} -&gt; "
                    f"{time_correction['new_taken_at']} by "
                    f"{escape(str(time_correction['actor']))} at "
                    f"{time_correction['corrected_at']}: "
                    f"{escape(str(time_correction['reason']))}",
                    styles["Normal"],
                )
            )
        for exclusion in exclusions:
            elements.append(
                Paragraph(
                    f"{str(exclusion['action']).capitalize()} by "
                    f"{escape(str(exclusion['actor']))} at {exclusion['occurred_at']}: "
                    f"{escape(str(exclusion['reason']))}",
                    styles["Normal"],
                )
            )
    if not any_events:
        elements.append(
            Paragraph("No corrections or exclusions recorded in this period.", styles["Normal"])
        )
    return elements


def _build_provenance_section(summary: CycleSummary, styles) -> list:
    """Build the device/provenance summary (addendum 9.2)."""
    elements: list = [Paragraph("Device and provenance summary", styles["Heading2"])]
    if summary.devices:
        elements.append(
            Paragraph("Devices: " + ", ".join(escape(d) for d in summary.devices), styles["Normal"])
        )
    else:
        elements.append(
            Paragraph(
                "Devices: none (no BLE-imported readings in this period)", styles["Normal"]
            )
        )
    elements.append(
        Paragraph(f"Manually entered readings: {summary.manual_reading_count}", styles["Normal"])
    )
    return elements


def build_pdf(
    *,
    profile: str,
    range_start: str,
    range_end: str,
    chart_data: ChartData,
    summary: CycleSummary,
    readings: list[dict[str, object]],
    db_path: str,
    generated_at: datetime,
    generated_tz: str,
) -> bytes:
    """Render the chart-only report and return its raw PDF bytes.

    Rendered to an in-memory buffer rather than straight to disk, so the
    caller can compute the content digest from the exact bytes it then
    writes -- the digest and the file on disk can never drift apart.

    Args:
        profile: The profile (``assigned_profile``) this report covers.
        range_start: Covered period's first date.
        range_end: Covered period's last date.
        chart_data: Pre-shaped data from ``chart.build_chart_data``, shared
            between the rendered chart and the daily detail table.
        summary: The page-1 cycle summary.
        readings: The readings covered by this report (for the daily table
            and provenance/audit sections).
        db_path: Filesystem path to the SQLite database file (for pulling
            correction/exclusion audit history).
        generated_at: The instant this report was generated (UTC).
        generated_tz: IANA timezone name used to display ``generated_at``.

    Returns:
        The rendered PDF's raw bytes.
    """
    styles = getSampleStyleSheet()
    buffer = BytesIO()
    # 0.5in side margins (vs. SimpleDocTemplate's 1in default) leave more
    # room for the chart's aligned context tracks/legend, which need real
    # width once a cycle runs to 3-4+ weeks of daily columns.
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, leftMargin=0.5 * inch, rightMargin=0.5 * inch
    )
    # SimpleDocTemplate's page Frame applies its own default 6pt left/right
    # padding on top of leftMargin/rightMargin (reportlab.platypus.Frame's
    # default leftPadding/rightPadding) -- doc.width alone is 12pt wider
    # than what a flowable can actually use before its right edge is
    # clipped at the frame boundary. Sizing the chart from doc.width - 12
    # keeps it exactly as wide as the page can render, regardless of the
    # margins chosen above.
    chart_width = doc.width - 12

    local_generated = generated_at.astimezone(ZoneInfo(generated_tz))
    elements: list = [
        Paragraph("Basal Body Temperature Report", styles["Title"]),
        Paragraph(f"Person: {escape(profile)}", styles["Normal"]),
        Paragraph(f"Covered period: {range_start} to {range_end}", styles["Normal"]),
        Paragraph(
            f"Generated {local_generated.strftime('%Y-%m-%d %H:%M %Z')}", styles["Normal"]
        ),
        Paragraph(
            "<b>Status: Chart-only.</b> No interpretation method is applied to this data. "
            "This report does not calculate or display a coverline, temperature shift, "
            "fertile-window opening/closing, or ovulation day.",
            styles["Normal"],
        ),
        Spacer(1, 0.15 * inch),
        render_chart(chart_data, width=chart_width),
        Spacer(1, 0.15 * inch),
        Paragraph("Cycle summary", styles["Heading2"]),
        Paragraph(_summary_text(summary), styles["Normal"]),
        PageBreak(),
        Paragraph("Daily detail", styles["Heading2"]),
    ]

    readings_by_id = {int(r["id"]): r for r in readings}
    elements.append(_build_daily_table(chart_data.days, readings_by_id))
    elements.append(Spacer(1, 0.2 * inch))
    elements.extend(_build_audit_section(db_path, chart_data.days, styles))
    elements.append(Spacer(1, 0.2 * inch))
    elements.extend(_build_provenance_section(summary, styles))

    doc.build(elements)
    return buffer.getvalue()


def generate_report(
    db_path: str,
    *,
    profile: str,
    start_date: str | None = None,
    end_date: str | None = None,
    output_path: str | None = None,
    timezone_name: str = "UTC",
    supersedes: int | None = None,
    cli_version: str = __version__,
) -> dict[str, object]:
    """Generate one chart-only report revision end to end and record it.

    Follows the addendum-10 status lifecycle on the ``reports`` row: the
    revision is recorded as ``"pending"`` before rendering starts, then
    ``"ready"`` (with its content digest and file path) or ``"failed"``
    once rendering finishes -- see ``storage.update_report_status``. If
    ``supersedes`` is given and generation succeeds, the prior revision is
    marked superseded only at that point, never before (so a failed
    regeneration never stops the old, still-valid report from being used).

    Args:
        db_path: Filesystem path to the SQLite database file.
        profile: The profile (``assigned_profile``) this report covers.
        start_date: Explicit inclusive start date, or None to auto-detect a
            cycle from ``cycle_start`` markers (see ``resolve_report_range``).
        end_date: Explicit inclusive end date; required together with
            ``start_date``.
        output_path: Destination PDF path; derived from the profile and
            date range if omitted.
        timezone_name: IANA timezone used to display the generation
            timestamp on page 1, and stored as ``generated_tz``.
        supersedes: A prior report id this revision replaces, if any.
        cli_version: Recorded in ``generation_params`` for traceability.

    Returns:
        The recorded report row (``storage.get_report``'s shape), including
        ``covered_reading_ids``.

    Raises:
        ReportError: If the date range can't be resolved.
    """
    ensure_schema(db_path)
    range_start, range_end, range_kind = resolve_report_range(
        db_path, profile, start_date=start_date, end_date=end_date
    )

    readings = fetch_report_readings(db_path, profile, range_start, range_end)
    context_entries = fetch_report_context_entries(db_path, profile, range_start, range_end)
    chart_data = build_chart_data(readings, context_entries, range_start, range_end)

    output = output_path or f"bbt-report-{profile}-{range_start}-to-{range_end}.pdf"
    generation_params = {
        "profile": profile,
        "range_start": range_start,
        "range_end": range_end,
        "range_kind": range_kind,
        "mode": "chart_only",
        "cli_version": cli_version,
    }
    generated_at_dt = datetime.now(timezone.utc)

    report_id = record_report(
        db_path,
        mode="chart_only",
        assigned_profile=profile,
        range_start=range_start,
        range_end=range_end,
        generation_params=generation_params,
        generated_tz=timezone_name,
        covered_reading_ids=[int(r["id"]) for r in readings],
        status="pending",
        generated_at=generated_at_dt.isoformat(),
        supersedes=supersedes,
    )

    try:
        summary = build_cycle_summary(
            db_path, chart_data, range_kind, range_start, range_end, readings
        )
        pdf_bytes = build_pdf(
            profile=profile,
            range_start=range_start,
            range_end=range_end,
            chart_data=chart_data,
            summary=summary,
            readings=readings,
            db_path=db_path,
            generated_at=generated_at_dt,
            generated_tz=timezone_name,
        )
    except Exception:
        update_report_status(db_path, report_id, "failed")
        raise

    digest = hashlib.sha256(pdf_bytes).hexdigest()
    output_file = Path(output)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(pdf_bytes)

    update_report_status(
        db_path, report_id, "ready", content_digest=digest, file_path=str(output_file)
    )
    if supersedes is not None:
        supersede_report(db_path, supersedes, report_id)

    report = get_report(db_path, report_id)
    assert report is not None  # just inserted/updated above
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="easyathome-bbt-report",
        description=(
            "Generate an immutable chart-only BBT PDF report from the daemon's reading "
            "database. No interpretation method (Sensiplan/SymptoPro/TCOYF) exists yet "
            "-- see docs/HEALTH_HUB_BBT_DAEMON_ADDENDUM.md."
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "-c", "--config", help="Path to the daemon's INI config file (reads db_path from it)"
    )
    source.add_argument(
        "-d", "--db", help="Path to the SQLite database file, bypassing the config file"
    )
    parser.add_argument(
        "-p", "--profile", required=True, help="Profile (assigned_profile) to report on"
    )
    parser.add_argument(
        "-s", "--start", dest="start_date", metavar="YYYY-MM-DD",
        help="Explicit start date, overrides cycle auto-detection",
    )
    parser.add_argument(
        "-e", "--end", dest="end_date", metavar="YYYY-MM-DD",
        help="Explicit end date (inclusive); required together with --start",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output PDF path (default: bbt-report-<profile>-<range>.pdf)",
    )
    parser.add_argument(
        "-z", "--timezone", dest="timezone_name",
        help=(
            "IANA timezone for the displayed generation time (default: "
            "daemon.device_timezone from --config, else UTC)"
        ),
    )
    parser.add_argument(
        "-r", "--supersedes", type=int, metavar="REPORT_ID",
        help="Report id this new revision replaces",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)

    db_path = args.db
    timezone_name = args.timezone_name
    if args.config:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"Error: {exc}")
            return 1
        db_path = config.db_path
        if timezone_name is None:
            timezone_name = config.device_timezone
    if timezone_name is None:
        timezone_name = "UTC"

    try:
        report = generate_report(
            db_path,
            profile=args.profile,
            start_date=args.start_date,
            end_date=args.end_date,
            output_path=args.output,
            timezone_name=timezone_name,
            supersedes=args.supersedes,
        )
    except (ReportError, StorageError) as exc:
        print(f"Error: {exc}")
        return 1

    print(
        f"Wrote report revision {report['revision']} (id {report['id']}, "
        f"status {report['status']}) to {report['file_path']}"
    )
    print(
        f"Covered {len(report['covered_reading_ids'])} reading(s), "
        f"digest {report['content_digest']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
