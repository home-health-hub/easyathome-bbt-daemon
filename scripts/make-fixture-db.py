#!/usr/bin/env python3
"""Create a small synthetic SQLite database for chart/report smoke testing.

There is no real EBT-300 hardware available (see the README warning
banner), so ``chart.py`` and ``report.py`` are exercised against fixture
data generated here instead. Unlike the sibling ``health-thermometer-daemon``
fixture script, this one imports and calls the real ``storage.py`` functions
(``record_ble_reading``, ``upsert_context_entry``, ``correct_reading``,
``exclude_reading``, ...) rather than hand-writing INSERT statements, so the
generated data exercises the exact same append-only audit-table code paths
a real correction/exclusion would.

Produces just over three weeks of one profile's readings with:

- A ``cycle_start`` marker on day 1.
- One missing day (a chart gap).
- One excluded reading (visible, unconnected).
- One corrected reading (original value retained, corrected value charted).
- Varied ``cervical_mucus``/``lh_test_result``/menstrual flow context.
- At least one day with multiple disturbance flags set.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from easyathome_bbt_daemon.storage import (  # noqa: E402
    assign_reading,
    correct_reading,
    ensure_schema,
    exclude_reading,
    record_ble_reading,
    upsert_context_entry,
)

PROFILE = "alice"
ADDRESS = "AA:BB:CC:DD:EE:FF"
TIMEZONE = "America/New_York"
START = date(2026, 7, 1)
CYCLE_DAYS = 24

# day-offset -> (menstrual_flow, cervical_mucus, lh_test_result)
_CONTEXT_BY_DAY = {
    0: ("heavy", "dry", "negative"),
    1: ("heavy", "dry", "negative"),
    2: ("medium", "dry", "negative"),
    3: ("light", "sticky", "negative"),
    4: ("spotting", "sticky", "negative"),
    5: (None, "creamy", "negative"),
    6: (None, "creamy", "negative"),
    7: (None, "watery", "negative"),
    8: (None, "watery", "negative"),
    9: (None, "eggwhite", "positive"),
    10: (None, "eggwhite", "positive"),
    11: (None, "dry", "negative"),
    12: (None, "dry", "negative"),
}

# day-offset -> disturbance flags to set (illness_fever, stress, shift_work,
# alcohol, travel_timezone_change, sleep_interrupted)
_DISTURBANCES_BY_DAY = {
    6: {"stress": 1},
    14: {"illness_fever": 1, "stress": 1},
}

#: Day offset whose reading is excluded, and why.
_EXCLUDED_DAY = 14
#: Day offset whose reading is corrected, and its corrected value.
_CORRECTED_DAY = 10
_CORRECTED_VALUE = 36.9
#: Day offset with no reading at all (a chart gap).
_MISSING_DAY = 17


def _taken_at(day_offset: int) -> tuple[str, str]:
    """Return (device_taken_at_raw, taken_at) for a 06:30 local waking reading."""
    naive = f"{(START + timedelta(days=day_offset)).isoformat()}T06:30:00"
    # Fixed -04:00 offset (America/New_York, EDT) -- avoids a zoneinfo
    # dependency in this simple generator; close enough for synthetic data.
    return naive, f"{naive}-04:00"


def build_fixture(db_path: str) -> None:
    """Populate ``db_path`` with the synthetic fixture described above."""
    ensure_schema(db_path)

    upsert_context_entry(
        db_path,
        START.isoformat(),
        assigned_profile=PROFILE,
        cycle_start=1,
        cycle_day=1,
    )

    base_temp = 36.4
    for offset in range(CYCLE_DAYS):
        entry_date = (START + timedelta(days=offset)).isoformat()
        flow, mucus, lh = _CONTEXT_BY_DAY.get(offset, (None, None, "negative"))
        fields: dict[str, object] = {"assigned_profile": PROFILE, "cycle_day": offset + 1}
        if flow is not None:
            fields["menstrual_flow"] = flow
        if mucus is not None:
            fields["cervical_mucus"] = mucus
        if lh is not None:
            fields["lh_test_result"] = lh
        fields.update(_DISTURBANCES_BY_DAY.get(offset, {}))
        upsert_context_entry(db_path, entry_date, **fields)

        if offset == _MISSING_DAY:
            continue  # deliberate gap: no reading recorded this day

        # Post-ovulation-ish rise after the LH-positive days, purely for a
        # visually plausible synthetic curve -- not a real interpretation.
        value = base_temp + (0.4 if offset >= 11 else 0.0) + (offset % 3) * 0.02
        device_taken_at_raw, taken_at = _taken_at(offset)
        result = record_ble_reading(
            db_path,
            ble_address=ADDRESS,
            device_taken_at_raw=device_taken_at_raw,
            device_taken_at_tz_assumption=TIMEZONE,
            taken_at=taken_at,
            received_at=taken_at,
            delivery_mode="live",
            value=round(value, 2),
        )
        assign_reading(db_path, result.reading_id, profile_ref=PROFILE, actor="fixture-script")

        if offset == _CORRECTED_DAY:
            correct_reading(
                db_path,
                result.reading_id,
                new_value=_CORRECTED_VALUE,
                new_unit="C",
                reason="Misread thermometer display at time of entry",
                actor="fixture-script",
            )
        if offset == _EXCLUDED_DAY:
            exclude_reading(
                db_path,
                result.reading_id,
                reason="Fever that day, not representative of baseline",
                actor="fixture-script",
            )


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} DB_PATH")
        return 1
    build_fixture(sys.argv[1])
    print(f"Wrote fixture data for profile {PROFILE!r} to {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
