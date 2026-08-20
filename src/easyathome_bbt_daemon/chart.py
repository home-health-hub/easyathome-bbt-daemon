"""Chart-only single-cycle rendering (addendum sections 7.1 and 8.1).

This module never calculates a coverline, temperature shift, fertile-window
opening/closing, ovulation day, or fertile/infertile status -- not even a
stub or placeholder for one. Chart-only mode (addendum 7.1) means exactly
that: raw observations and context, plotted, with nothing derived from them.
If a future interpretation engine (Sensiplan, SymptoPro, TCOYF) is added, it
belongs in its own isolated module (addendum 7.2), never here.

Two responsibilities are deliberately kept separate:

- ``build_chart_data`` shapes daemon rows (``readings``, ``context_entries``)
  into a plain-dataclass ``ChartData`` -- no reportlab import is needed to
  exercise this function, which is what makes it possible to unit test the
  gap/exclusion/segment logic without also asserting on drawing internals.
- ``render_chart`` turns a ``ChartData`` into a reportlab ``Drawing``.

``report.py`` calls ``build_chart_data`` once and derives both the rendered
chart *and* the daily detail table/summary from the same ``ChartData.days``
list, so the two projections cannot disagree (addendum 9.3's "dashboard and
PDF projections must use the same reporting/query layer").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from reportlab.graphics.shapes import Circle, Drawing, Line, Rect, String
from reportlab.lib import colors

_UNIT_LABELS = {"C": "°C", "F": "°F"}

#: (context_entries column, one-letter code) for the aligned "Disturbances"
#: track and its legend. Order is fixed/schema-driven, unlike the
#: menstrual-flow/mucus/LH abbreviations below, which are built from
#: whatever free-text values actually appear in the data.
_DISTURBANCE_FIELDS = (
    ("illness_fever", "I"),
    ("stress", "S"),
    ("shift_work", "W"),
    ("alcohol", "A"),
    ("travel_timezone_change", "T"),
    ("sleep_interrupted", "Z"),
)
_OTHER_DISTURBANCE_CODE = "O"

_MARKER_COLOR = colors.HexColor("#2f5d8a")
_EXCLUDED_COLOR = colors.HexColor("#cc0000")
_DISTURBED_RING_COLOR = colors.HexColor("#e69138")

_LEFT_MARGIN = 55.0
_RIGHT_MARGIN = 20.0
_PLOT_HEIGHT = 160.0
_ROW_HEIGHT = 12.0
_TITLE_HEIGHT = 18.0
_LEGEND_ROW_HEIGHT = 11.0
_LEGEND_ROWS = 3
_TOP_PAD = 10.0
_BOTTOM_PAD = 6.0
#: Two lines (abbreviation legend, disturbance-code legend) -- combined
#: onto one line the text is long enough to overflow even a wide page at
#: readable font sizes, since a Drawing's shapes aren't clipped to its
#: declared width; splitting them keeps each line comfortably shorter.
_ABBR_LEGEND_HEIGHT = 26.0
_GAP = 8.0


@dataclass
class DayPoint:
    """One calendar day's charted temperature and aligned context, or gaps thereof.

    ``value``/``reading_id`` are None when no reading exists for this date
    (a gap, addendum 8.1) -- never interpolated or filled in.
    """

    date: str
    cycle_day: int | None
    value: float | None
    unit: str | None
    is_excluded: bool
    is_disturbed: bool
    reading_id: int | None
    menstrual_flow: str | None
    cervical_mucus: str | None
    lh_test_result: str | None
    disturbance_flags: list[str]


@dataclass
class ChartData:
    """Pre-shaped chart inputs, independent of any reportlab rendering."""

    days: list[DayPoint]
    #: Each inner list is a run of ``days`` indices to connect with straight
    #: lines -- addendum 8.1's "straight connections between consecutive
    #: valid readings", never crossing a gap or an excluded day. Runs of
    #: length 1 are omitted (nothing to connect).
    segments: list[list[int]]
    #: Track label -> one short display string per day, aligned to ``days``.
    context_rows: dict[str, list[str]] = field(default_factory=dict)
    unit: str | None = None
    #: (abbreviation, original value) pairs actually seen in this range, for
    #: an explicit, data-driven legend (addendum 8.2 accessibility
    #: requirement: no color-only encoding).
    abbreviation_legend: list[tuple[str, str]] = field(default_factory=list)


def _disturbance_flags(context: dict[str, object] | None) -> list[str]:
    """Return the names of every active disturbance flag on a context entry."""
    if context is None:
        return []
    flags = [name for name, _code in _DISTURBANCE_FIELDS if context.get(name)]
    if context.get("other_disturbance_flags"):
        flags.append("other_disturbance_flags")
    return flags


def _disturbance_code(flags: list[str]) -> str:
    """Render active disturbance flags as a compact code, e.g. "IS" or "-"."""
    if not flags:
        return "-"
    code_map = dict(_DISTURBANCE_FIELDS)
    return "".join(code_map.get(name, _OTHER_DISTURBANCE_CODE) for name in flags)


def _abbreviate(value: str | None) -> str:
    """Return a one-character uppercase abbreviation, or "-" if not entered."""
    return value[0].upper() if value else "-"


def _format_short_date(iso_date: str) -> str:
    """Render an ISO date as compact "M/D" for the aligned Date track."""
    parsed = date.fromisoformat(iso_date)
    return f"{parsed.month}/{parsed.day}"


def build_chart_data(
    readings: list[dict[str, object]],
    context_entries: list[dict[str, object]],
    start_date: str,
    end_date: str,
) -> ChartData:
    """Shape reading/context rows into per-day chart data over a date range.

    Args:
        readings: Rows as returned by ``storage.list_readings`` (or a
            pre-filtered subset), already restricted to the profile and
            period of interest by the caller.
        context_entries: Rows as returned by ``storage.list_context_entries``,
            similarly pre-filtered.
        start_date: Inclusive first date of the range (``YYYY-MM-DD``).
        end_date: Inclusive last date of the range (``YYYY-MM-DD``).

    Returns:
        A ``ChartData`` with one ``DayPoint`` per calendar day in range.
    """
    readings_by_date: dict[str, dict[str, object]] = {}
    for reading in readings:
        day_str = datetime.fromisoformat(str(reading["taken_at"])).date().isoformat()
        existing = readings_by_date.get(day_str)
        # Addendum 8.1 wants one point per daily reading. This daemon has no
        # "primary reading of the day" flag yet (addendum 12.2 lists
        # multiple-readings-in-one-day as an expected case to handle, not
        # one it prescribes a resolution for) -- the earliest taken_at that
        # day is charted, since BBT charting conventionally cares about the
        # first waking measurement.
        if existing is None or str(reading["taken_at"]) < str(existing["taken_at"]):
            readings_by_date[day_str] = reading

    context_by_date = {str(entry["entry_date"]): entry for entry in context_entries}

    days: list[DayPoint] = []
    current = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    while current <= last:
        day_str = current.isoformat()
        reading = readings_by_date.get(day_str)
        context = context_by_date.get(day_str)
        flags = _disturbance_flags(context)
        cycle_day = context.get("cycle_day") if context else None
        value = float(reading["charting_value"]) if reading else None
        unit = str(reading["charting_unit"]) if reading else None
        reading_id = int(reading["id"]) if reading else None
        menstrual_flow = context.get("menstrual_flow") if context else None
        cervical_mucus = context.get("cervical_mucus") if context else None
        lh_test_result = context.get("lh_test_result") if context else None
        days.append(
            DayPoint(
                date=day_str,
                cycle_day=cycle_day,
                value=value,
                unit=unit,
                is_excluded=bool(reading["is_excluded"]) if reading else False,
                is_disturbed=bool(flags),
                reading_id=reading_id,
                menstrual_flow=menstrual_flow,
                cervical_mucus=cervical_mucus,
                lh_test_result=lh_test_result,
                disturbance_flags=flags,
            )
        )
        current += timedelta(days=1)

    return ChartData(
        days=days,
        segments=_build_segments(days),
        context_rows=_build_context_rows(days),
        unit=next((day.unit for day in days if day.unit), None),
        abbreviation_legend=_build_abbreviation_legend(days),
    )


def _build_segments(days: list[DayPoint]) -> list[list[int]]:
    """Group day indices into connectable runs: consecutive, valid, not excluded.

    A single missing day or a single excluded day breaks the run -- no
    interpolation across gaps (addendum 8.1), and excluded points are never
    connected by a line even though they remain visible (addendum 8.1's
    "visible but unconnected excluded readings").
    """
    segments: list[list[int]] = []
    current_run: list[int] = []
    for index, day in enumerate(days):
        connectable = day.value is not None and not day.is_excluded
        if connectable:
            current_run.append(index)
            continue
        if len(current_run) > 1:
            segments.append(current_run)
        current_run = []
    if len(current_run) > 1:
        segments.append(current_run)
    return segments


def _build_context_rows(days: list[DayPoint]) -> dict[str, list[str]]:
    """Build the aligned text tracks shown below the temperature plot (addendum 8.2)."""
    return {
        "Date": [_format_short_date(day.date) for day in days],
        "Cycle day": [str(day.cycle_day) if day.cycle_day is not None else "-" for day in days],
        "Menstrual flow": [_abbreviate(day.menstrual_flow) for day in days],
        "Cervical mucus": [_abbreviate(day.cervical_mucus) for day in days],
        "LH test": [_abbreviate(day.lh_test_result) for day in days],
        "Disturbances": [_disturbance_code(day.disturbance_flags) for day in days],
    }


def _build_abbreviation_legend(days: list[DayPoint]) -> list[tuple[str, str]]:
    """Collect (abbreviation, original value) pairs actually present, for the legend.

    Built from the data itself rather than a fixed taxonomy, since
    ``menstrual_flow``/``cervical_mucus``/``lh_test_result`` are free-text
    columns (addendum 5.1), not enums.
    """
    seen: dict[str, str] = {}
    for day in days:
        for value in (day.menstrual_flow, day.cervical_mucus, day.lh_test_result):
            if value:
                seen.setdefault(_abbreviate(value), value)
    return sorted(seen.items())


def render_chart(chart_data: ChartData, *, width: float = 540.0) -> Drawing:
    """Render ``chart_data`` as a reportlab ``Drawing``.

    Draws exactly what chart-only mode allows (addendum 7.1/8.1): a
    temperature point per day with the value present, straight lines only
    between immediately-consecutive valid days, visibly distinct but
    unconnected excluded points, a distinct marker for disturbed readings,
    the aligned context tracks below the plot, and an explicit legend. No
    coverline, shift, fertile window, or ovulation marker is computed or
    drawn -- see the module docstring.

    Args:
        chart_data: Pre-shaped data from ``build_chart_data``.
        width: Drawing width in points.

    Returns:
        A ``reportlab.graphics.shapes.Drawing``.
    """
    days = chart_data.days
    values = [day.value for day in days if day.value is not None]
    track_names = list(chart_data.context_rows)

    legend_h = _LEGEND_ROWS * _LEGEND_ROW_HEIGHT
    tracks_h = len(track_names) * _ROW_HEIGHT
    total_height = (
        _TOP_PAD + _TITLE_HEIGHT + _PLOT_HEIGHT + _GAP + legend_h + _GAP
        + tracks_h + _GAP + _ABBR_LEGEND_HEIGHT + _BOTTOM_PAD
    )
    drawing = Drawing(width, total_height)

    if not values:
        drawing.add(String(10, total_height / 2, "No temperature readings in this range."))
        return drawing

    plot_width = width - _LEFT_MARGIN - _RIGHT_MARGIN
    value_span = max(values) - min(values)
    buffer = max(0.3, value_span * 0.15) if value_span else 0.3
    y_min, y_max = min(values) - buffer, max(values) + buffer

    plot_bottom = _BOTTOM_PAD + _ABBR_LEGEND_HEIGHT + _GAP + tracks_h + _GAP + legend_h + _GAP
    plot_top = plot_bottom + _PLOT_HEIGHT

    def x_at(index: int) -> float:
        if len(days) == 1:
            return _LEFT_MARGIN + plot_width / 2
        return _LEFT_MARGIN + plot_width * index / (len(days) - 1)

    def y_at(value: float) -> float:
        return plot_bottom + (value - y_min) / (y_max - y_min) * _PLOT_HEIGHT

    unit_label = _UNIT_LABELS.get(chart_data.unit or "", chart_data.unit or "")
    drawing.add(
        String(
            _LEFT_MARGIN,
            plot_top + _TITLE_HEIGHT - 12,
            f"Temperature ({unit_label}) -- chart-only: no coverline, shift, "
            "fertile window, or ovulation marker",
            fontName="Helvetica-Bold",
            fontSize=8.5,
        )
    )

    drawing.add(Line(_LEFT_MARGIN, plot_bottom, _LEFT_MARGIN, plot_top, strokeColor=colors.black))
    drawing.add(
        Line(
            _LEFT_MARGIN, plot_bottom, _LEFT_MARGIN + plot_width, plot_bottom,
            strokeColor=colors.black,
        )
    )
    drawing.add(String(2, plot_top - 4, f"{y_max:.1f}", fontSize=7))
    drawing.add(String(2, plot_bottom, f"{y_min:.1f}", fontSize=7))

    for segment in chart_data.segments:
        for start_index, end_index in zip(segment, segment[1:]):
            start_day, end_day = days[start_index], days[end_index]
            drawing.add(
                Line(
                    x_at(start_index), y_at(start_day.value),  # type: ignore[arg-type]
                    x_at(end_index), y_at(end_day.value),  # type: ignore[arg-type]
                    strokeColor=_MARKER_COLOR, strokeWidth=1.2,
                )
            )

    for index, day in enumerate(days):
        if day.value is None:
            continue
        x, y = x_at(index), y_at(day.value)
        if day.is_excluded:
            _draw_excluded_marker(drawing, x, y)
        elif day.is_disturbed:
            _draw_disturbed_marker(drawing, x, y)
        else:
            drawing.add(Circle(x, y, 2.6, fillColor=_MARKER_COLOR, strokeColor=_MARKER_COLOR))

    _add_marker_legend(drawing, _LEFT_MARGIN, plot_bottom - _GAP)
    tracks_top = plot_bottom - _GAP - legend_h - _GAP
    _add_context_tracks(drawing, chart_data, x_at, tracks_top)

    return drawing


def _draw_excluded_marker(drawing: Drawing, x: float, y: float) -> None:
    """Open circle with an X through it -- visible, but shape-distinct from a normal point."""
    drawing.add(Circle(x, y, 3.2, strokeColor=_EXCLUDED_COLOR, fillColor=None, strokeWidth=1.2))
    drawing.add(
        Line(x - 2.2, y - 2.2, x + 2.2, y + 2.2, strokeColor=_EXCLUDED_COLOR, strokeWidth=1)
    )
    drawing.add(
        Line(x - 2.2, y + 2.2, x + 2.2, y - 2.2, strokeColor=_EXCLUDED_COLOR, strokeWidth=1)
    )


def _draw_disturbed_marker(drawing: Drawing, x: float, y: float) -> None:
    """Filled circle ringed by an open square -- shape-distinct from a normal point."""
    drawing.add(Circle(x, y, 2.6, fillColor=_MARKER_COLOR, strokeColor=_MARKER_COLOR))
    drawing.add(
        Rect(
            x - 4.5, y - 4.5, 9, 9,
            strokeColor=_DISTURBED_RING_COLOR, fillColor=None, strokeWidth=1.1,
        )
    )


def _add_marker_legend(drawing: Drawing, x: float, top_y: float) -> None:
    """Draw the marker-shape legend (addendum 8.2: explicit legend, not color alone)."""
    entries = ("normal", "disturbed", "excluded")
    labels = {
        "normal": "Reading",
        "disturbed": "Disturbed reading",
        "excluded": "Excluded reading (not connected)",
    }
    for i, kind in enumerate(entries):
        row_y = top_y - i * _LEGEND_ROW_HEIGHT
        cx, cy = x + 4, row_y + 3
        if kind == "normal":
            drawing.add(Circle(cx, cy, 2.6, fillColor=_MARKER_COLOR, strokeColor=_MARKER_COLOR))
        elif kind == "disturbed":
            _draw_disturbed_marker(drawing, cx, cy)
        else:
            _draw_excluded_marker(drawing, cx, cy)
        drawing.add(
            String(x + 14, row_y, labels[kind], fontName="Helvetica", fontSize=7)
        )


def _add_context_tracks(
    drawing: Drawing, chart_data: ChartData, x_at, top_y: float
) -> None:
    """Draw the aligned per-day text tracks and their legend below the plot."""
    for row_index, (track_name, values) in enumerate(chart_data.context_rows.items()):
        row_y = top_y - row_index * _ROW_HEIGHT
        drawing.add(String(2, row_y, track_name, fontName="Helvetica-Bold", fontSize=6.5))
        for day_index, value in enumerate(values):
            drawing.add(
                String(x_at(day_index) - 3, row_y, value, fontName="Helvetica", fontSize=6.5)
            )

    # Two separate lines, not one combined string: joined together the full
    # legend text is long enough to run off the page at a readable font
    # size, since a Drawing's shapes are never clipped to its declared
    # width -- see _ABBR_LEGEND_HEIGHT.
    first_line_y = top_y - len(chart_data.context_rows) * _ROW_HEIGHT - 8
    second_line_y = first_line_y - 9

    disturbance_legend = (
        "Disturbances: I=illness/fever S=stress W=shift work A=alcohol "
        "T=travel/tz change Z=interrupted sleep O=other"
    )
    if chart_data.abbreviation_legend:
        abbr_legend = "Legend: " + "; ".join(
            f"{abbr}={full}" for abbr, full in chart_data.abbreviation_legend
        )
        drawing.add(String(2, max(first_line_y, 11), abbr_legend, fontName="Helvetica", fontSize=6))
        drawing.add(
            String(2, max(second_line_y, 2), disturbance_legend, fontName="Helvetica", fontSize=6)
        )
    else:
        drawing.add(
            String(2, max(first_line_y, 2), "Legend: " + disturbance_legend,
                   fontName="Helvetica", fontSize=6)
        )
