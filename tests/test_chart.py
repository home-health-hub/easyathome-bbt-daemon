from reportlab.graphics.shapes import Drawing

from easyathome_bbt_daemon.chart import build_chart_data, render_chart

PROFILE = "alice"


def _reading(reading_id, taken_at, value, *, is_excluded=False):
    return {
        "id": reading_id,
        "taken_at": taken_at,
        "charting_value": value,
        "charting_unit": "C",
        "is_excluded": 1 if is_excluded else 0,
    }


def _context(entry_date, **overrides):
    entry = {
        "entry_date": entry_date,
        "assigned_profile": PROFILE,
        "cycle_day": None,
        "menstrual_flow": None,
        "cervical_mucus": None,
        "lh_test_result": None,
        "illness_fever": 0,
        "stress": 0,
        "shift_work": 0,
        "alcohol": 0,
        "travel_timezone_change": 0,
        "sleep_interrupted": 0,
        "other_disturbance_flags": None,
    }
    entry.update(overrides)
    return entry


# --- build_chart_data: pure data shaping, no reportlab involved -------------


def test_one_day_per_calendar_day_in_range():
    readings = [_reading(1, "2026-07-01T06:30:00-04:00", 36.4)]
    context = [_context("2026-07-01", cycle_day=1)]
    chart_data = build_chart_data(readings, context, "2026-07-01", "2026-07-03")
    assert [day.date for day in chart_data.days] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert chart_data.days[0].value == 36.4
    assert chart_data.days[0].cycle_day == 1
    # No reading/context on 07-02 or 07-03 -- these are gaps, not zeros.
    assert chart_data.days[1].value is None
    assert chart_data.days[2].cycle_day is None


def test_gap_breaks_the_connecting_segment():
    readings = [
        _reading(1, "2026-07-01T06:30:00-04:00", 36.4),
        # 07-02 missing entirely
        _reading(2, "2026-07-03T06:30:00-04:00", 36.5),
        _reading(3, "2026-07-04T06:30:00-04:00", 36.6),
    ]
    chart_data = build_chart_data(readings, [], "2026-07-01", "2026-07-04")
    # Day 0 is isolated (day 1 is a gap) so it can't connect to anything;
    # days 2-3 (07-03, 07-04) are consecutive and do connect.
    assert chart_data.segments == [[2, 3]]


def test_excluded_reading_visible_but_not_connected():
    readings = [
        _reading(1, "2026-07-01T06:30:00-04:00", 36.4),
        _reading(2, "2026-07-02T06:30:00-04:00", 39.0, is_excluded=True),
        _reading(3, "2026-07-03T06:30:00-04:00", 36.5),
    ]
    chart_data = build_chart_data(readings, [], "2026-07-01", "2026-07-03")
    # The excluded day still has a value (visible)...
    assert chart_data.days[1].value == 39.0
    assert chart_data.days[1].is_excluded is True
    # ...but no segment connects across it -- day 0 and day 2 are each
    # isolated single points, not merged into one run.
    assert chart_data.segments == []


def test_no_interpolation_no_line_across_multi_day_gap():
    readings = [
        _reading(1, "2026-07-01T06:30:00-04:00", 36.4),
        _reading(2, "2026-07-05T06:30:00-04:00", 36.6),
    ]
    chart_data = build_chart_data(readings, [], "2026-07-01", "2026-07-05")
    assert chart_data.segments == []  # both points isolated, nothing connects


def test_consecutive_valid_days_form_one_segment():
    readings = [
        _reading(1, "2026-07-01T06:30:00-04:00", 36.4),
        _reading(2, "2026-07-02T06:30:00-04:00", 36.5),
        _reading(3, "2026-07-03T06:30:00-04:00", 36.6),
    ]
    chart_data = build_chart_data(readings, [], "2026-07-01", "2026-07-03")
    assert chart_data.segments == [[0, 1, 2]]


def test_multiple_readings_same_day_uses_earliest():
    readings = [
        _reading(1, "2026-07-01T06:30:00-04:00", 36.4),
        _reading(2, "2026-07-01T14:00:00-04:00", 37.1),  # later same-day reading
    ]
    chart_data = build_chart_data(readings, [], "2026-07-01", "2026-07-01")
    assert chart_data.days[0].value == 36.4
    assert chart_data.days[0].reading_id == 1


def test_disturbance_flags_and_code():
    context = [_context("2026-07-01", illness_fever=1, stress=1)]
    chart_data = build_chart_data([], context, "2026-07-01", "2026-07-01")
    day = chart_data.days[0]
    assert day.is_disturbed is True
    assert set(day.disturbance_flags) == {"illness_fever", "stress"}
    assert chart_data.context_rows["Disturbances"][0] == "IS"


def test_no_disturbance_flags_renders_dash():
    context = [_context("2026-07-01")]
    chart_data = build_chart_data([], context, "2026-07-01", "2026-07-01")
    assert chart_data.context_rows["Disturbances"][0] == "-"


def test_abbreviation_legend_built_from_data_present():
    context = [
        _context("2026-07-01", menstrual_flow="heavy", cervical_mucus="dry"),
        _context("2026-07-02", menstrual_flow="light"),
    ]
    chart_data = build_chart_data([], context, "2026-07-01", "2026-07-02")
    legend = dict(chart_data.abbreviation_legend)
    assert legend["H"] == "heavy"
    assert legend["D"] == "dry"
    assert legend["L"] == "light"


def test_unit_taken_from_first_reading_with_a_value():
    readings = [_reading(1, "2026-07-01T06:30:00-04:00", 36.4)]
    chart_data = build_chart_data(readings, [], "2026-07-01", "2026-07-01")
    assert chart_data.unit == "C"


def test_no_readings_at_all_has_empty_segments_and_none_unit():
    chart_data = build_chart_data([], [], "2026-07-01", "2026-07-02")
    assert chart_data.segments == []
    assert chart_data.unit is None
    assert all(day.value is None for day in chart_data.days)


# --- render_chart: only asserting it renders without error, not pixels ------


def test_render_chart_produces_a_drawing():
    readings = [
        _reading(1, "2026-07-01T06:30:00-04:00", 36.4),
        _reading(2, "2026-07-02T06:30:00-04:00", 39.0, is_excluded=True),
        _reading(3, "2026-07-03T06:30:00-04:00", 36.6),
    ]
    context = [_context("2026-07-01", cycle_day=1, menstrual_flow="heavy", stress=1)]
    chart_data = build_chart_data(readings, context, "2026-07-01", "2026-07-03")
    drawing = render_chart(chart_data)
    assert isinstance(drawing, Drawing)
    assert len(drawing.contents) > 0


def test_render_chart_handles_no_readings():
    chart_data = build_chart_data([], [], "2026-07-01", "2026-07-02")
    drawing = render_chart(chart_data)
    assert isinstance(drawing, Drawing)


def test_render_chart_handles_single_day():
    readings = [_reading(1, "2026-07-01T06:30:00-04:00", 36.4)]
    chart_data = build_chart_data(readings, [], "2026-07-01", "2026-07-01")
    drawing = render_chart(chart_data)
    assert isinstance(drawing, Drawing)
