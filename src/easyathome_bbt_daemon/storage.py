"""SQLite storage backend for BBT observations and daily context entries.

Implements the persistence model from
``docs/HEALTH_HUB_BBT_DAEMON_ADDENDUM.md`` sections 4 and 6: stable
daemon-owned reading identity with idempotent dedup, the
``device_taken_at_raw`` / ``taken_at`` / ``received_at`` / ``imported_at``
time model, append-only correction and exclusion audit trails (not
UPDATE-in-place-only columns), revisable assignment, and a schema for the
daily context-entry fields from section 5.1 (CRUD only -- the entry form
itself is a later phase).

Two intentional design choices worth calling out for anyone extending this
module:

- ``devices.ble_address`` is the only device identity ``easyathome-ble``
  and the EBT-300 actually expose (no serial number). Per addendum 4.2,
  BLE address alone must not be assumed a permanent identity, so readings
  reference a daemon-owned integer ``device_id`` rather than the address
  string directly -- if a future device generation exposes a real serial,
  or an address needs to be re-mapped, only the ``devices`` row changes,
  not every historical reading.
- Corrections and exclusions are never plain UPDATE-in-place: each has its
  own append-only history table. The current value is *also* cached
  directly on ``readings``/``context_entries`` (``corrected_value``,
  ``charting_value``, ``is_excluded``, ...) purely so ordinary queries
  don't need a correlated subquery -- that cache is always derived from,
  and kept in sync with, the audit table. The audit table is the source of
  truth; the cached column is a read optimization.

The ``reports`` table (addendum section 9.3) follows a related but distinct
pattern: each row *is* an immutable revision, not a cache of one. Superseding
a report never deletes or rewrites an old row's own generation facts
(``generated_at``, ``content_digest``, ``file_path``, ...) -- ``supersede_report``
only ever adds a forward-pointing ``superseded_by`` link and flips ``status``
to ``"superseded"`` on the old row, exactly as a new correction never touches
a reading's ``original_value``. See ``CLAUDE.md`` for the full reasoning.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ble_address TEXT UNIQUE NOT NULL,
    label TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_uid TEXT UNIQUE NOT NULL,
    device_id INTEGER REFERENCES devices(id),
    source_type TEXT NOT NULL,
    delivery_mode TEXT,
    raw_provenance_digest TEXT,
    device_taken_at_raw TEXT,
    device_taken_at_tz_assumption TEXT,
    taken_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    imported_at TEXT,
    original_value REAL NOT NULL,
    original_unit TEXT NOT NULL,
    corrected_value REAL,
    corrected_unit TEXT,
    charting_value REAL NOT NULL,
    charting_unit TEXT NOT NULL,
    measurement_method TEXT,
    is_excluded INTEGER NOT NULL DEFAULT 0,
    assigned_profile TEXT,
    assigned_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reading_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id INTEGER NOT NULL REFERENCES readings(id),
    previous_value REAL NOT NULL,
    previous_unit TEXT NOT NULL,
    new_value REAL NOT NULL,
    new_unit TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    corrected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reading_time_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id INTEGER NOT NULL REFERENCES readings(id),
    previous_taken_at TEXT NOT NULL,
    new_taken_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    corrected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reading_exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id INTEGER NOT NULL REFERENCES readings(id),
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reading_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id INTEGER NOT NULL REFERENCES readings(id),
    profile_ref TEXT,
    actor TEXT NOT NULL,
    assigned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date TEXT UNIQUE NOT NULL,
    assigned_profile TEXT,
    menstrual_flow TEXT,
    cycle_start INTEGER,
    cycle_day INTEGER,
    lh_test_result TEXT,
    pregnancy_test_result TEXT,
    cervical_mucus TEXT,
    cervical_observation TEXT,
    measurement_method TEXT,
    sleep_duration_minutes INTEGER,
    sleep_interrupted INTEGER,
    time_deviation_minutes INTEGER,
    illness_fever INTEGER,
    stress INTEGER,
    shift_work INTEGER,
    alcohol INTEGER,
    travel_timezone_change INTEGER,
    medication TEXT,
    other_disturbance_flags TEXT,
    notes TEXT,
    is_excluded INTEGER NOT NULL DEFAULT 0,
    exclusion_reason TEXT,
    correction_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_entry_exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    context_entry_id INTEGER NOT NULL REFERENCES context_entries(id),
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_uid TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    assigned_profile TEXT,
    range_start TEXT,
    range_end TEXT,
    generation_params TEXT,
    generated_at TEXT NOT NULL,
    generated_tz TEXT NOT NULL,
    content_digest TEXT,
    file_path TEXT,
    supersedes INTEGER REFERENCES reports(id),
    superseded_by INTEGER REFERENCES reports(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_covered_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id),
    reading_id INTEGER NOT NULL REFERENCES readings(id)
);
"""

#: Valid values for reports.status, per addendum section 10.
REPORT_STATUSES = ("pending", "ready", "failed", "superseded")

#: Editable context-entry columns, i.e. everything except the primary key,
#: the unique entry_date, and the bookkeeping/audit-cache columns that have
#: their own dedicated functions (is_excluded/exclusion_reason via
#: exclude_context_entry, created_at/updated_at set automatically).
_CONTEXT_ENTRY_FIELDS = (
    "assigned_profile",
    "menstrual_flow",
    "cycle_start",
    "cycle_day",
    "lh_test_result",
    "pregnancy_test_result",
    "cervical_mucus",
    "cervical_observation",
    "measurement_method",
    "sleep_duration_minutes",
    "sleep_interrupted",
    "time_deviation_minutes",
    "illness_fever",
    "stress",
    "shift_work",
    "alcohol",
    "travel_timezone_change",
    "medication",
    "other_disturbance_flags",
    "notes",
    "correction_reason",
)


class StorageError(Exception):
    """Raised for storage-layer misuse, e.g. referencing an unknown reading."""


@dataclass
class RecordResult:
    """Outcome of a BLE-import record attempt.

    Attributes:
        reading_id: Primary key of the (possibly pre-existing) reading row.
        created: True if this call inserted a new row; False if an
            identical reading was already stored (idempotent dedup -- see
            ``compute_reading_uid``) and no new row was created.
    """

    reading_id: int
    created: bool


def _utc_now_iso() -> str:
    """Return the current instant as a UTC ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db_path: str) -> None:
    """Create every table if it doesn't already exist.

    Safe to call from any entry point (collector, API server, tests)
    regardless of whether the database file already exists.

    Args:
        db_path: Filesystem path to the SQLite database file. Parent
            directories are created automatically if missing.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_SCHEMA)
        connection.commit()
    finally:
        connection.close()


def resolve_naive_local_time(naive_iso: str, tz_name: str, fold: int = 0) -> str:
    """Attach a timezone to a naive device-reported timestamp.

    ``easyathome-ble`` hands back a timezone-naive ``datetime`` for every
    measurement (the EBT-300 protocol carries plain year/month/day/hour/
    minute/second fields with no offset). This resolves that naive local
    time into an absolute instant under an assumed IANA timezone, without
    mutating the original value -- callers are expected to keep the naive
    string as ``device_taken_at_raw`` and store *this* function's output as
    ``taken_at``.

    Args:
        naive_iso: A naive ISO-8601 string (no offset), e.g.
            ``"2026-11-01T01:30:00"``.
        tz_name: IANA timezone name to interpret ``naive_iso`` under, e.g.
            ``"America/New_York"``.
        fold: Disambiguates a local time that occurs twice during a
            fall-back DST transition (0 = first/earlier occurrence, the
            Python default; 1 = second/later occurrence). Ignored for
            unambiguous times. Has no effect on a spring-forward gap time
            (which ``zoneinfo`` normalizes rather than rejecting) -- if
            that distinction ever matters, it needs handling upstream of
            this function.

    Returns:
        An ISO-8601 string with UTC offset representing the resolved
        instant.
    """
    naive_dt = datetime.fromisoformat(naive_iso)
    aware_dt = naive_dt.replace(tzinfo=ZoneInfo(tz_name), fold=fold)
    return aware_dt.isoformat()


def compute_reading_uid(
    ble_address: str, device_taken_at_raw: str | None, delivery_mode: str | None, value: float
) -> str:
    """Compute a stable, deterministic identifier for a BLE-imported reading.

    Per addendum 4.2, identity should consider the device identifier, the
    device-provided timestamp, a raw-message digest, and the message
    type/source stream. ``easyathome-ble``'s notification handler only
    forwards a parsed ``TemperatureMeasurement`` to its callback (see
    ``collector.py``'s module docstring) -- the raw 15-byte notification
    itself is not exposed -- so this digest is computed over the most
    granular fields actually available: device address, the device's own
    (naive, pre-timezone-resolution) timestamp, live-vs-historical delivery
    mode, and the reported temperature. Redelivering the identical physical
    reading (a reconnect, or a repeated historical-history download)
    reproduces the same inputs and therefore the same digest, which is what
    makes ``record_ble_reading`` idempotent via the ``readings.reading_uid``
    UNIQUE constraint.

    Known limitation: if the *same* physical measurement is ever delivered
    once tagged live and later again tagged historical (or vice versa) with
    a timestamp that doesn't match to the second, this digest treats them
    as two distinct readings, since the library exposes no correlating
    sequence number. Revisit once real hardware fixtures are available to
    confirm whether that scenario actually occurs.

    Args:
        ble_address: The device's BLE address.
        device_taken_at_raw: The device's naive timestamp as an ISO-8601
            string (no offset), or None if unavailable.
        delivery_mode: ``"live"`` or ``"historical"``.
        value: The reported temperature value (Celsius, as the device and
            library always report it).

    Returns:
        A hex-encoded SHA-256 digest string.
    """
    canonical = f"{ble_address}|{device_taken_at_raw}|{delivery_mode}|{value:.2f}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_or_create_device(db_path: str, ble_address: str, label: str | None = None) -> int:
    """Return the daemon-owned device id for a BLE address, creating it if new.

    Updates ``last_seen_at`` on every call (including for an existing
    device), so the row also tracks whether a device has gone quiet.

    Args:
        db_path: Filesystem path to the SQLite database file.
        ble_address: The device's BLE address.
        label: Optional human-readable label, only set on first creation.

    Returns:
        The device's daemon-owned integer id.
    """
    now = _utc_now_iso()
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(
            "SELECT id FROM devices WHERE ble_address = ?", (ble_address,)
        )
        row = cursor.fetchone()
        if row is not None:
            connection.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?", (now, row[0])
            )
            connection.commit()
            return row[0]

        cursor = connection.execute(
            "INSERT INTO devices (ble_address, label, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (ble_address, label, now, now),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def record_ble_reading(
    db_path: str,
    *,
    ble_address: str,
    device_taken_at_raw: str | None,
    device_taken_at_tz_assumption: str | None,
    taken_at: str,
    received_at: str,
    delivery_mode: str,
    value: float,
    unit: str = "C",
    measurement_method: str | None = None,
    imported_at: str | None = None,
) -> RecordResult:
    """Persist one BLE-imported observation, idempotently.

    This is the persistence-order-critical call (addendum 4.1): it must
    succeed and commit on its own, independent of whether any later context
    entry is ever saved. It opens and commits its own connection/transaction
    and never participates in a transaction shared with context-entry
    writes.

    Args:
        db_path: Filesystem path to the SQLite database file.
        ble_address: The device's BLE address (resolved to a device_id via
            ``get_or_create_device``).
        device_taken_at_raw: The device's own naive local timestamp, as an
            ISO-8601 string with no offset, exactly as parsed -- never
            rewritten by a later correction.
        device_taken_at_tz_assumption: The timezone name (or other
            description) applied to interpret ``device_taken_at_raw`` into
            ``taken_at``, stored for auditability.
        taken_at: The resolved measurement instant, as an ISO-8601 string
            with UTC offset (see ``resolve_naive_local_time``).
        received_at: The daemon's receipt time, as a UTC ISO-8601 string.
        delivery_mode: ``"live"`` or ``"historical"``.
        value: Temperature value exactly as the device/library reported it
            -- no conversion at storage time.
        unit: Unit of ``value``. ``easyathome-ble`` always reports Celsius,
            but this isn't hardcoded so a future library version reporting
            Fahrenheit doesn't require a schema change.
        measurement_method: Body-site/method label, if known (the EBT-300
            doesn't report one itself; this is normally set later via
            context entry, so usually None at collection time).
        imported_at: Only set when a distinct later database-import step
            (not live/historical BLE collection) produced this row. Must
            not be conflated with ``received_at``.

    Returns:
        A ``RecordResult`` noting whether a new row was created or an
        identical reading already existed (dedup).
    """
    device_id = get_or_create_device(db_path, ble_address)
    reading_uid = compute_reading_uid(ble_address, device_taken_at_raw, delivery_mode, value)
    now = _utc_now_iso()

    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO readings (
                reading_uid, device_id, source_type, delivery_mode,
                raw_provenance_digest, device_taken_at_raw,
                device_taken_at_tz_assumption, taken_at, received_at,
                imported_at, original_value, original_unit, corrected_value,
                corrected_unit, charting_value, charting_unit,
                measurement_method, is_excluded, assigned_profile,
                assigned_at, created_at
            ) VALUES (?, ?, 'ble_import', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, 0,
                      NULL, NULL, ?)
            """,
            (
                reading_uid,
                device_id,
                delivery_mode,
                reading_uid,
                device_taken_at_raw,
                device_taken_at_tz_assumption,
                taken_at,
                received_at,
                imported_at,
                value,
                unit,
                value,
                unit,
                measurement_method,
                now,
            ),
        )
        connection.commit()

        if cursor.rowcount > 0:
            return RecordResult(reading_id=cursor.lastrowid, created=True)

        existing = connection.execute(
            "SELECT id FROM readings WHERE reading_uid = ?", (reading_uid,)
        ).fetchone()
        return RecordResult(reading_id=existing[0], created=False)
    finally:
        connection.close()


def record_manual_reading(
    db_path: str,
    *,
    taken_at: str,
    received_at: str,
    value: float,
    unit: str,
    actor: str,
    measurement_method: str | None = None,
) -> int:
    """Persist one manually entered observation (no BLE device involved).

    Manual entries have no device-provided digest to dedup against -- each
    call always inserts a new row, on the assumption that a human
    intentionally entering a reading twice meant to record two readings,
    not that reconnect/resync logic accidentally replayed one.

    Args:
        db_path: Filesystem path to the SQLite database file.
        taken_at: The measurement instant the person reports, as an
            ISO-8601 string with UTC offset.
        received_at: When the daemon recorded the entry, as a UTC
            ISO-8601 string.
        value: Temperature value as entered.
        unit: Unit of ``value`` ("C" or "F").
        actor: Who/what entered this reading (e.g. a person's name or
            account identifier), stored for audit purposes even though
            manual readings have no correction/exclusion event yet.
        measurement_method: Body-site/method label, if provided.

    Returns:
        The inserted row's primary key.
    """
    reading_uid = f"manual:{uuid.uuid4()}"
    now = _utc_now_iso()
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(
            """
            INSERT INTO readings (
                reading_uid, device_id, source_type, delivery_mode,
                raw_provenance_digest, device_taken_at_raw,
                device_taken_at_tz_assumption, taken_at, received_at,
                imported_at, original_value, original_unit, corrected_value,
                corrected_unit, charting_value, charting_unit,
                measurement_method, is_excluded, assigned_profile,
                assigned_at, created_at
            ) VALUES (?, NULL, 'manual', NULL, NULL, NULL, NULL, ?, ?, NULL, ?, ?, NULL, NULL,
                      ?, ?, ?, 0, NULL, NULL, ?)
            """,
            (
                reading_uid,
                taken_at,
                received_at,
                value,
                unit,
                value,
                unit,
                measurement_method,
                now,
            ),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def _row_to_reading_dict(row: sqlite3.Row) -> dict[str, object]:
    """Convert a ``readings`` row into a plain dict keyed by column name."""
    return dict(row)


def get_reading(db_path: str, reading_id: int) -> dict[str, object] | None:
    """Fetch one reading by id.

    Args:
        db_path: Filesystem path to the SQLite database file.
        reading_id: The reading's primary key.

    Returns:
        A dict of every column, or None if no row matches.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM readings WHERE id = ?", (reading_id,)
        ).fetchone()
        return _row_to_reading_dict(row) if row is not None else None
    finally:
        connection.close()


def list_readings(
    db_path: str,
    *,
    include_excluded: bool = True,
    assigned_profile: str | None = None,
) -> list[dict[str, object]]:
    """List readings, most recently taken first.

    Args:
        db_path: Filesystem path to the SQLite database file.
        include_excluded: If False, omit currently-excluded readings.
        assigned_profile: Restrict to readings currently assigned to this
            profile reference, if given.

    Returns:
        One dict per matching reading.
    """
    query = "SELECT * FROM readings WHERE 1=1"
    params: list[object] = []
    if not include_excluded:
        query += " AND is_excluded = 0"
    if assigned_profile is not None:
        query += " AND assigned_profile = ?"
        params.append(assigned_profile)
    query += " ORDER BY taken_at DESC"

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(query, params).fetchall()
        return [_row_to_reading_dict(row) for row in rows]
    finally:
        connection.close()


def correct_reading(
    db_path: str,
    reading_id: int,
    *,
    new_value: float,
    new_unit: str,
    reason: str,
    actor: str,
    corrected_at: str | None = None,
) -> None:
    """Record a value correction and update the cached charting value.

    Appends to ``reading_corrections`` (retaining the prior value, unit,
    and reason) and updates ``readings.corrected_value``/``corrected_unit``/
    ``charting_value``/``charting_unit`` to the new value. The original
    device-reported value in ``original_value``/``original_unit`` is never
    touched -- it remains the immutable evidence addendum 6 requires.

    Args:
        db_path: Filesystem path to the SQLite database file.
        reading_id: The reading to correct.
        new_value: The corrected temperature value.
        new_unit: Unit of ``new_value``.
        reason: Required human-readable reason for the correction.
        actor: Who/what made the correction.
        corrected_at: ISO-8601 timestamp of the correction; defaults to now
            (UTC) if omitted.

    Raises:
        StorageError: If no reading matches ``reading_id``.
    """
    if not reason:
        raise StorageError("A correction reason is required")

    when = corrected_at or _utc_now_iso()
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT corrected_value, corrected_unit, original_value, original_unit "
            "FROM readings WHERE id = ?",
            (reading_id,),
        ).fetchone()
        if row is None:
            raise StorageError(f"No reading with id {reading_id}")

        previous_value = row[0] if row[0] is not None else row[2]
        previous_unit = row[1] if row[1] is not None else row[3]

        connection.execute(
            "INSERT INTO reading_corrections "
            "(reading_id, previous_value, previous_unit, new_value, new_unit, reason, "
            "actor, corrected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (reading_id, previous_value, previous_unit, new_value, new_unit, reason, actor, when),
        )
        connection.execute(
            "UPDATE readings SET corrected_value = ?, corrected_unit = ?, "
            "charting_value = ?, charting_unit = ? WHERE id = ?",
            (new_value, new_unit, new_value, new_unit, reading_id),
        )
        connection.commit()
    finally:
        connection.close()


def get_reading_corrections(db_path: str, reading_id: int) -> list[dict[str, object]]:
    """Return the full value-correction history for a reading, oldest first."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM reading_corrections WHERE reading_id = ? ORDER BY id ASC",
            (reading_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def correct_reading_time(
    db_path: str,
    reading_id: int,
    *,
    new_taken_at: str,
    reason: str,
    actor: str,
    corrected_at: str | None = None,
) -> None:
    """Record a revision to a reading's resolved ``taken_at`` instant.

    Covers device clock correction, a DST-transition reinterpretation, or a
    discovered travel/timezone-change error -- anything that revises when a
    reading actually happened without touching ``device_taken_at_raw``
    (which stays exactly as the device reported it) or the temperature
    value itself.

    Args:
        db_path: Filesystem path to the SQLite database file.
        reading_id: The reading whose resolved time is being corrected.
        new_taken_at: The corrected instant, as an ISO-8601 string with UTC
            offset.
        reason: Required human-readable reason for the correction.
        actor: Who/what made the correction.
        corrected_at: ISO-8601 timestamp of the correction; defaults to now
            (UTC) if omitted.

    Raises:
        StorageError: If no reading matches ``reading_id``.
    """
    if not reason:
        raise StorageError("A time correction reason is required")

    when = corrected_at or _utc_now_iso()
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT taken_at FROM readings WHERE id = ?", (reading_id,)
        ).fetchone()
        if row is None:
            raise StorageError(f"No reading with id {reading_id}")

        connection.execute(
            "INSERT INTO reading_time_corrections "
            "(reading_id, previous_taken_at, new_taken_at, reason, actor, corrected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (reading_id, row[0], new_taken_at, reason, actor, when),
        )
        connection.execute(
            "UPDATE readings SET taken_at = ? WHERE id = ?", (new_taken_at, reading_id)
        )
        connection.commit()
    finally:
        connection.close()


def get_reading_time_corrections(db_path: str, reading_id: int) -> list[dict[str, object]]:
    """Return the full taken_at-correction history for a reading, oldest first."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM reading_time_corrections WHERE reading_id = ? ORDER BY id ASC",
            (reading_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def exclude_reading(
    db_path: str, reading_id: int, *, reason: str, actor: str, occurred_at: str | None = None
) -> None:
    """Mark a reading excluded and append an audit event.

    Args:
        db_path: Filesystem path to the SQLite database file.
        reading_id: The reading to exclude.
        reason: Required human-readable reason.
        actor: Who/what excluded it.
        occurred_at: ISO-8601 timestamp; defaults to now (UTC).

    Raises:
        StorageError: If no reading matches ``reading_id``.
    """
    _append_reading_exclusion_event(db_path, reading_id, "excluded", reason, actor, occurred_at)


def reverse_reading_exclusion(
    db_path: str, reading_id: int, *, reason: str, actor: str, occurred_at: str | None = None
) -> None:
    """Un-exclude a previously excluded reading, retaining the reversal in history.

    Args:
        db_path: Filesystem path to the SQLite database file.
        reading_id: The reading to un-exclude.
        reason: Required human-readable reason for the reversal.
        actor: Who/what reversed the exclusion.
        occurred_at: ISO-8601 timestamp; defaults to now (UTC).

    Raises:
        StorageError: If no reading matches ``reading_id``.
    """
    _append_reading_exclusion_event(db_path, reading_id, "reversed", reason, actor, occurred_at)


def _append_reading_exclusion_event(
    db_path: str,
    reading_id: int,
    action: str,
    reason: str,
    actor: str,
    occurred_at: str | None,
) -> None:
    """Shared implementation for ``exclude_reading``/``reverse_reading_exclusion``."""
    if not reason:
        raise StorageError("An exclusion/reversal reason is required")

    when = occurred_at or _utc_now_iso()
    is_excluded = 1 if action == "excluded" else 0
    connection = sqlite3.connect(db_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM readings WHERE id = ?", (reading_id,)
        ).fetchone()
        if exists is None:
            raise StorageError(f"No reading with id {reading_id}")

        connection.execute(
            "INSERT INTO reading_exclusions (reading_id, action, reason, actor, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (reading_id, action, reason, actor, when),
        )
        connection.execute(
            "UPDATE readings SET is_excluded = ? WHERE id = ?", (is_excluded, reading_id)
        )
        connection.commit()
    finally:
        connection.close()


def get_reading_exclusion_history(db_path: str, reading_id: int) -> list[dict[str, object]]:
    """Return the full exclude/reverse history for a reading, oldest first."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM reading_exclusions WHERE reading_id = ? ORDER BY id ASC",
            (reading_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def assign_reading(
    db_path: str,
    reading_id: int,
    *,
    profile_ref: str | None,
    actor: str,
    assigned_at: str | None = None,
) -> None:
    """Record a Health-Hub-coordinated assignment (or reassignment/un-assignment).

    Per addendum 4.5, assignment is a persisted, later-revisable
    association, not a destructive overwrite -- every assignment action
    (including clearing it, via ``profile_ref=None``) is appended to
    ``reading_assignments`` so the full assignment history stays
    inspectable, while ``readings.assigned_profile`` caches the current
    value for ordinary queries.

    Args:
        db_path: Filesystem path to the SQLite database file.
        reading_id: The reading being (re)assigned.
        profile_ref: The Health-Hub-provided profile identifier, or None to
            explicitly clear the assignment (still recorded as an event).
        actor: Who/what performed the assignment (typically "health-hub" or
            a specific coordinating identity).
        assigned_at: ISO-8601 timestamp; defaults to now (UTC).

    Raises:
        StorageError: If no reading matches ``reading_id``.
    """
    when = assigned_at or _utc_now_iso()
    connection = sqlite3.connect(db_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM readings WHERE id = ?", (reading_id,)
        ).fetchone()
        if exists is None:
            raise StorageError(f"No reading with id {reading_id}")

        connection.execute(
            "INSERT INTO reading_assignments (reading_id, profile_ref, actor, assigned_at) "
            "VALUES (?, ?, ?, ?)",
            (reading_id, profile_ref, actor, when),
        )
        connection.execute(
            "UPDATE readings SET assigned_profile = ?, assigned_at = ? WHERE id = ?",
            (profile_ref, when, reading_id),
        )
        connection.commit()
    finally:
        connection.close()


def get_reading_assignment_history(db_path: str, reading_id: int) -> list[dict[str, object]]:
    """Return the full assignment history for a reading, oldest first."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM reading_assignments WHERE reading_id = ? ORDER BY id ASC",
            (reading_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def upsert_context_entry(db_path: str, entry_date: str, **fields: object) -> int:
    """Create or partially update a day's context entry (autosave-friendly).

    Only the fields actually passed in ``fields`` are changed; omitted
    fields keep their previously stored value (or NULL, "not entered", on
    first creation). This matches the addendum 5.2 "partial-entry autosave"
    requirement -- a phone-friendly form can save one answer at a time
    without clobbering the others.

    ``NULL`` is the "not entered" sentinel throughout. Callers wanting a
    distinct "unknown" or "none" value must pass that literal string (e.g.
    ``menstrual_flow="none"`` vs. ``menstrual_flow="unknown"`` vs. simply
    omitting the field for "not entered") -- this function does not itself
    interpret field values, it only decides which columns to touch.

    Args:
        db_path: Filesystem path to the SQLite database file.
        entry_date: The calendar date this entry covers, as an ISO-8601
            ``YYYY-MM-DD`` string. One entry per date.
        **fields: Any subset of the context-entry columns (see
            ``_CONTEXT_ENTRY_FIELDS``).

    Returns:
        The entry's primary key.

    Raises:
        StorageError: If an unknown field name is passed.
    """
    unknown = set(fields) - set(_CONTEXT_ENTRY_FIELDS)
    if unknown:
        raise StorageError(f"Unknown context entry field(s): {sorted(unknown)}")

    now = _utc_now_iso()
    connection = sqlite3.connect(db_path)
    try:
        existing = connection.execute(
            "SELECT id FROM context_entries WHERE entry_date = ?", (entry_date,)
        ).fetchone()

        if existing is None:
            columns = ["entry_date", "created_at", "updated_at", *fields.keys()]
            placeholders = ", ".join("?" for _ in columns)
            values = [entry_date, now, now, *fields.values()]
            cursor = connection.execute(
                f"INSERT INTO context_entries ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
            connection.commit()
            return cursor.lastrowid

        if fields:
            set_clause = ", ".join(f"{name} = ?" for name in fields)
            connection.execute(
                f"UPDATE context_entries SET {set_clause}, updated_at = ? WHERE id = ?",
                [*fields.values(), now, existing[0]],
            )
        else:
            connection.execute(
                "UPDATE context_entries SET updated_at = ? WHERE id = ?", (now, existing[0])
            )
        connection.commit()
        return existing[0]
    finally:
        connection.close()


def get_context_entry(db_path: str, entry_date: str) -> dict[str, object] | None:
    """Fetch one context entry by date.

    Args:
        db_path: Filesystem path to the SQLite database file.
        entry_date: The calendar date, as ``YYYY-MM-DD``.

    Returns:
        A dict of every column, or None if no entry exists for that date.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM context_entries WHERE entry_date = ?", (entry_date,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def list_context_entries(
    db_path: str, *, start_date: str | None = None, end_date: str | None = None
) -> list[dict[str, object]]:
    """List context entries within an optional date range, oldest first.

    Args:
        db_path: Filesystem path to the SQLite database file.
        start_date: Inclusive lower bound (``YYYY-MM-DD``), if given.
        end_date: Inclusive upper bound (``YYYY-MM-DD``), if given.

    Returns:
        One dict per matching entry, ordered by ``entry_date`` ascending.
    """
    query = "SELECT * FROM context_entries WHERE 1=1"
    params: list[str] = []
    if start_date is not None:
        query += " AND entry_date >= ?"
        params.append(start_date)
    if end_date is not None:
        query += " AND entry_date <= ?"
        params.append(end_date)
    query += " ORDER BY entry_date ASC"

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def exclude_context_entry(
    db_path: str, entry_date: str, *, reason: str, actor: str, occurred_at: str | None = None
) -> None:
    """Mark a context entry excluded and append an audit event.

    Args:
        db_path: Filesystem path to the SQLite database file.
        entry_date: The entry's date, as ``YYYY-MM-DD``.
        reason: Required human-readable reason.
        actor: Who/what excluded it.
        occurred_at: ISO-8601 timestamp; defaults to now (UTC).

    Raises:
        StorageError: If no entry exists for ``entry_date``.
    """
    _append_context_exclusion_event(db_path, entry_date, "excluded", reason, actor, occurred_at)


def reverse_context_entry_exclusion(
    db_path: str, entry_date: str, *, reason: str, actor: str, occurred_at: str | None = None
) -> None:
    """Un-exclude a previously excluded context entry, retaining the reversal in history.

    Args:
        db_path: Filesystem path to the SQLite database file.
        entry_date: The entry's date, as ``YYYY-MM-DD``.
        reason: Required human-readable reason for the reversal.
        actor: Who/what reversed the exclusion.
        occurred_at: ISO-8601 timestamp; defaults to now (UTC).

    Raises:
        StorageError: If no entry exists for ``entry_date``.
    """
    _append_context_exclusion_event(db_path, entry_date, "reversed", reason, actor, occurred_at)


def _append_context_exclusion_event(
    db_path: str,
    entry_date: str,
    action: str,
    reason: str,
    actor: str,
    occurred_at: str | None,
) -> None:
    """Shared implementation for context-entry exclude/reverse functions."""
    if not reason:
        raise StorageError("An exclusion/reversal reason is required")

    when = occurred_at or _utc_now_iso()
    is_excluded = 1 if action == "excluded" else 0
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT id FROM context_entries WHERE entry_date = ?", (entry_date,)
        ).fetchone()
        if row is None:
            raise StorageError(f"No context entry for date {entry_date}")

        connection.execute(
            "INSERT INTO context_entry_exclusions "
            "(context_entry_id, action, reason, actor, occurred_at) VALUES (?, ?, ?, ?, ?)",
            (row[0], action, reason, actor, when),
        )
        connection.execute(
            "UPDATE context_entries SET is_excluded = ?, exclusion_reason = ? WHERE id = ?",
            (is_excluded, reason, row[0]),
        )
        connection.commit()
    finally:
        connection.close()


def get_context_entry_exclusion_history(
    db_path: str, entry_date: str
) -> list[dict[str, object]]:
    """Return the full exclude/reverse history for a context entry, oldest first."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT id FROM context_entries WHERE entry_date = ?", (entry_date,)
        ).fetchone()
        if row is None:
            return []
        rows = connection.execute(
            "SELECT * FROM context_entry_exclusions WHERE context_entry_id = ? ORDER BY id ASC",
            (row[0],),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def get_device(db_path: str, device_id: int) -> dict[str, object] | None:
    """Fetch one device row by its daemon-owned id, for report provenance summaries."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def record_report(
    db_path: str,
    *,
    mode: str,
    assigned_profile: str | None,
    range_start: str,
    range_end: str,
    generation_params: dict[str, object],
    generated_tz: str,
    covered_reading_ids: list[int],
    status: str = "pending",
    generated_at: str | None = None,
    supersedes: int | None = None,
) -> int:
    """Insert a new, immutable report revision row.

    Per addendum 9.3, each generated PDF is its own revision -- this never
    overwrites a prior report row. ``report_uid`` identifies the *logical*
    report across its revisions: a fresh one is minted unless ``supersedes``
    names an earlier revision, in which case this row inherits that row's
    ``report_uid`` and takes the next ``revision`` number. The row starts in
    whatever ``status`` the caller passes (normally ``"pending"``, updated
    to ``"ready"``/``"failed"`` via ``update_report_status`` once PDF
    rendering finishes) -- see ``report.py``'s ``generate_report`` for the
    full lifecycle, including when ``supersede_report`` is called to mark
    the prior revision superseded.

    Args:
        db_path: Filesystem path to the SQLite database file.
        mode: Interpretation mode used, always ``"chart_only"`` this phase.
        assigned_profile: The profile this report covers, if any.
        range_start: Covered period's first date (``YYYY-MM-DD``).
        range_end: Covered period's last date (``YYYY-MM-DD``).
        generation_params: Arbitrary JSON-serializable dict of the inputs
            used to generate this report (profile, date range, mode, CLI
            version, ...), stored verbatim for later reproduction/audit.
        generated_tz: Timezone name used to display generation time on the
            rendered report.
        covered_reading_ids: Reading ids included in this report, recorded
            in ``report_covered_readings`` for addendum 9.3 reproducibility.
        status: Initial status; one of ``REPORT_STATUSES``.
        generated_at: ISO-8601 instant the report was generated; defaults
            to now (UTC).
        supersedes: The report id this revision replaces, if any.

    Returns:
        The new report row's primary key.

    Raises:
        StorageError: If ``status`` is invalid or ``supersedes`` names an
            unknown report.
    """
    if status not in REPORT_STATUSES:
        raise StorageError(f"Invalid report status {status!r}; must be one of {REPORT_STATUSES}")

    now = _utc_now_iso()
    when_generated = generated_at or now

    connection = sqlite3.connect(db_path)
    try:
        if supersedes is not None:
            prior = connection.execute(
                "SELECT report_uid, revision FROM reports WHERE id = ?", (supersedes,)
            ).fetchone()
            if prior is None:
                raise StorageError(f"No report with id {supersedes}")
            report_uid, revision = prior[0], prior[1] + 1
        else:
            report_uid, revision = str(uuid.uuid4()), 1

        cursor = connection.execute(
            """
            INSERT INTO reports (
                report_uid, revision, status, mode, assigned_profile, range_start,
                range_end, generation_params, generated_at, generated_tz,
                content_digest, file_path, supersedes, superseded_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?)
            """,
            (
                report_uid,
                revision,
                status,
                mode,
                assigned_profile,
                range_start,
                range_end,
                json.dumps(generation_params),
                when_generated,
                generated_tz,
                supersedes,
                now,
            ),
        )
        report_id = cursor.lastrowid

        connection.executemany(
            "INSERT INTO report_covered_readings (report_id, reading_id) VALUES (?, ?)",
            [(report_id, reading_id) for reading_id in covered_reading_ids],
        )
        connection.commit()
        return report_id
    finally:
        connection.close()


def update_report_status(
    db_path: str,
    report_id: int,
    status: str,
    *,
    content_digest: str | None = None,
    file_path: str | None = None,
) -> None:
    """Transition a report row's own generation attempt to a terminal state.

    This updates the row in place, but only ever the row's *own* in-flight
    generation attempt (``"pending"`` -> ``"ready"``/``"failed"``) -- it is
    not a way to rewrite a previously ``"ready"`` report. A change to
    underlying data after that point must go through ``record_report`` with
    ``supersedes`` set, producing a new revision rather than mutating this
    one (addendum 9.3).

    Args:
        db_path: Filesystem path to the SQLite database file.
        report_id: The report row to update.
        status: New status; one of ``REPORT_STATUSES``.
        content_digest: SHA-256 hex digest of the rendered PDF bytes, set
            when ``status == "ready"``.
        file_path: Filesystem path of the rendered PDF, set when
            ``status == "ready"``.

    Raises:
        StorageError: If ``status`` is invalid or no report matches
            ``report_id``.
    """
    if status not in REPORT_STATUSES:
        raise StorageError(f"Invalid report status {status!r}; must be one of {REPORT_STATUSES}")

    connection = sqlite3.connect(db_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        if exists is None:
            raise StorageError(f"No report with id {report_id}")

        connection.execute(
            "UPDATE reports SET status = ?, content_digest = ?, file_path = ? WHERE id = ?",
            (status, content_digest, file_path, report_id),
        )
        connection.commit()
    finally:
        connection.close()


def supersede_report(db_path: str, old_report_id: int, new_report_id: int) -> None:
    """Mark ``old_report_id`` as superseded by ``new_report_id``.

    Called once the new revision has actually reached ``"ready"`` -- not at
    the moment a new revision is merely requested -- so a failed
    regeneration attempt never strands the still-valid prior report in a
    superseded state with nothing to replace it.

    Args:
        db_path: Filesystem path to the SQLite database file.
        old_report_id: The previously current report revision.
        new_report_id: The revision that now supersedes it.

    Raises:
        StorageError: If ``old_report_id`` doesn't exist.
    """
    connection = sqlite3.connect(db_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM reports WHERE id = ?", (old_report_id,)
        ).fetchone()
        if exists is None:
            raise StorageError(f"No report with id {old_report_id}")

        connection.execute(
            "UPDATE reports SET status = 'superseded', superseded_by = ? WHERE id = ?",
            (new_report_id, old_report_id),
        )
        connection.commit()
    finally:
        connection.close()


def _row_to_report_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    """Convert a ``reports`` row into a dict, attaching its covered reading ids."""
    report = dict(row)
    covered = connection.execute(
        "SELECT reading_id FROM report_covered_readings WHERE report_id = ? ORDER BY reading_id",
        (report["id"],),
    ).fetchall()
    report["covered_reading_ids"] = [r[0] for r in covered]
    return report


def get_report(db_path: str, report_id: int) -> dict[str, object] | None:
    """Fetch one report revision by id, including its covered reading ids.

    Args:
        db_path: Filesystem path to the SQLite database file.
        report_id: The report row's primary key.

    Returns:
        A dict of every column plus ``covered_reading_ids``, or None if no
        row matches.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return _row_to_report_dict(connection, row) if row is not None else None
    finally:
        connection.close()


def list_reports_for_profile(
    db_path: str, assigned_profile: str | None, *, include_superseded: bool = True
) -> list[dict[str, object]]:
    """List report revisions for a profile, most recently generated first.

    Args:
        db_path: Filesystem path to the SQLite database file.
        assigned_profile: The profile to list reports for (matches
            ``reports.assigned_profile`` exactly, including None).
        include_superseded: If False, omit rows whose status is
            ``"superseded"``.

    Returns:
        One dict per matching report revision, each including
        ``covered_reading_ids``.
    """
    query = "SELECT * FROM reports WHERE assigned_profile IS ?"
    params: list[object] = [assigned_profile]
    if not include_superseded:
        query += " AND status != 'superseded'"
    query += " ORDER BY generated_at DESC"

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(query, params).fetchall()
        return [_row_to_report_dict(connection, row) for row in rows]
    finally:
        connection.close()


def get_report_history(db_path: str, report_uid: str) -> list[dict[str, object]]:
    """Return every revision of one logical report, oldest first.

    Args:
        db_path: Filesystem path to the SQLite database file.
        report_uid: The stable identifier shared by every revision of one
            logical report (see ``record_report``).

    Returns:
        One dict per revision, each including ``covered_reading_ids``.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM reports WHERE report_uid = ? ORDER BY revision ASC", (report_uid,)
        ).fetchall()
        return [_row_to_report_dict(connection, row) for row in rows]
    finally:
        connection.close()
