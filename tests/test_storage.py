from easyathome_bbt_daemon.storage import (
    StorageError,
    assign_reading,
    compute_reading_uid,
    correct_reading,
    correct_reading_time,
    ensure_schema,
    exclude_context_entry,
    exclude_reading,
    get_context_entry,
    get_context_entry_exclusion_history,
    get_or_create_device,
    get_reading,
    get_reading_assignment_history,
    get_reading_corrections,
    get_reading_exclusion_history,
    get_reading_time_corrections,
    list_context_entries,
    list_readings,
    record_ble_reading,
    record_manual_reading,
    resolve_naive_local_time,
    reverse_context_entry_exclusion,
    reverse_reading_exclusion,
    upsert_context_entry,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _db(tmp_path):
    """Return a path to a fresh, schema-initialized SQLite database.

    Mirrors what a real entry point does: cli.py calls ensure_schema()
    once before its run loop starts, rather than every storage function
    checking/creating the schema on each call.
    """
    db_path = str(tmp_path / "bbt.db")
    ensure_schema(db_path)
    return db_path


# --- naive device times / timezone resolution -------------------------------


def test_resolve_naive_local_time_unambiguous():
    resolved = resolve_naive_local_time("2026-06-15T06:30:00", "America/New_York")
    assert resolved == "2026-06-15T06:30:00-04:00"


def test_resolve_naive_local_time_dst_fallback_ambiguous():
    # 2026-11-01 01:30 America/New_York occurs twice during fall-back DST.
    early = resolve_naive_local_time("2026-11-01T01:30:00", "America/New_York", fold=0)
    late = resolve_naive_local_time("2026-11-01T01:30:00", "America/New_York", fold=1)
    assert early == "2026-11-01T01:30:00-04:00"
    assert late == "2026-11-01T01:30:00-05:00"
    assert early != late


def test_resolve_naive_local_time_dst_spring_forward():
    # Before and after the March 2026 spring-forward transition.
    before = resolve_naive_local_time("2026-03-08T01:30:00", "America/New_York")
    after = resolve_naive_local_time("2026-03-08T03:30:00", "America/New_York")
    assert before.endswith("-05:00")
    assert after.endswith("-04:00")


# --- stable identity / idempotent dedup --------------------------------------


def test_compute_reading_uid_deterministic():
    uid1 = compute_reading_uid(ADDRESS, "2026-01-01T07:00:00", "live", 36.7)
    uid2 = compute_reading_uid(ADDRESS, "2026-01-01T07:00:00", "live", 36.7)
    assert uid1 == uid2


def test_compute_reading_uid_differs_by_delivery_mode():
    live = compute_reading_uid(ADDRESS, "2026-01-01T07:00:00", "live", 36.7)
    historical = compute_reading_uid(ADDRESS, "2026-01-01T07:00:00", "historical", 36.7)
    assert live != historical


def test_duplicate_import_idempotency(tmp_path):
    db_path = _db(tmp_path)
    kwargs = dict(
        ble_address=ADDRESS,
        device_taken_at_raw="2026-01-01T07:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-01-01T07:00:00-05:00",
        received_at="2026-01-01T12:00:05+00:00",
        delivery_mode="historical",
        value=36.7,
    )
    first = record_ble_reading(db_path, **kwargs)
    assert first.created is True

    # Simulate a reconnect replaying the same historical download.
    second = record_ble_reading(db_path, **kwargs)
    assert second.created is False
    assert second.reading_id == first.reading_id

    assert len(list_readings(db_path)) == 1


def test_get_or_create_device_stable_across_calls(tmp_path):
    db_path = _db(tmp_path)
    first_id = get_or_create_device(db_path, ADDRESS)
    second_id = get_or_create_device(db_path, ADDRESS)
    assert first_id == second_id


# --- taken_at / received_at / device_taken_at_raw model ----------------------


def test_late_historical_import_received_days_after_taken(tmp_path):
    db_path = _db(tmp_path)
    result = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2025-12-20T06:45:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2025-12-20T06:45:00-05:00",
        received_at="2026-01-02T09:00:00+00:00",
        delivery_mode="historical",
        value=36.5,
    )
    reading = get_reading(db_path, result.reading_id)
    assert reading["device_taken_at_raw"] == "2025-12-20T06:45:00"
    assert reading["taken_at"] == "2025-12-20T06:45:00-05:00"
    assert reading["received_at"] == "2026-01-02T09:00:00+00:00"
    # received_at is ~13 days after taken_at; both must be preserved exactly.
    assert reading["taken_at"] != reading["received_at"]


def test_travel_timezone_change_reflected_in_taken_at(tmp_path):
    db_path = _db(tmp_path)
    # Same wall-clock reading time, but taken while traveling under a
    # different assumed timezone than usual -- device_taken_at_raw is
    # identical local digits, taken_at resolves to a different instant.
    home = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-02-10T06:30:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at=resolve_naive_local_time("2026-02-10T06:30:00", "America/New_York"),
        received_at="2026-02-10T11:30:10+00:00",
        delivery_mode="live",
        value=36.6,
    )
    traveling = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-02-11T06:30:00",
        device_taken_at_tz_assumption="Europe/London",
        taken_at=resolve_naive_local_time("2026-02-11T06:30:00", "Europe/London"),
        received_at="2026-02-11T06:30:10+00:00",
        delivery_mode="live",
        value=36.6,
    )
    home_row = get_reading(db_path, home.reading_id)
    travel_row = get_reading(db_path, traveling.reading_id)
    assert home_row["device_taken_at_tz_assumption"] == "America/New_York"
    assert travel_row["device_taken_at_tz_assumption"] == "Europe/London"
    assert home_row["taken_at"] != travel_row["taken_at"]


def test_device_clock_correction_retains_history(tmp_path):
    db_path = _db(tmp_path)
    result = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-03-01T07:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-03-01T07:00:00-05:00",
        received_at="2026-03-01T12:00:05+00:00",
        delivery_mode="live",
        value=36.8,
    )
    reading_id = result.reading_id

    correct_reading_time(
        db_path,
        reading_id,
        new_taken_at="2026-03-01T07:15:00-05:00",
        reason="Device clock was found to be 15 minutes fast",
        actor="household-admin",
    )

    reading = get_reading(db_path, reading_id)
    assert reading["taken_at"] == "2026-03-01T07:15:00-05:00"
    # device_taken_at_raw must never be silently rewritten.
    assert reading["device_taken_at_raw"] == "2026-03-01T07:00:00"

    history = get_reading_time_corrections(db_path, reading_id)
    assert len(history) == 1
    assert history[0]["previous_taken_at"] == "2026-03-01T07:00:00-05:00"
    assert history[0]["new_taken_at"] == "2026-03-01T07:15:00-05:00"
    assert history[0]["reason"] == "Device clock was found to be 15 minutes fast"


def test_correct_reading_time_requires_reason(tmp_path):
    db_path = _db(tmp_path)
    result = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-03-01T07:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-03-01T07:00:00-05:00",
        received_at="2026-03-01T12:00:05+00:00",
        delivery_mode="live",
        value=36.8,
    )
    try:
        correct_reading_time(
            db_path, result.reading_id, new_taken_at="2026-03-01T07:15:00-05:00",
            reason="", actor="someone",
        )
        assert False, "expected StorageError"
    except StorageError:
        pass


# --- persistence order: context-form failure must not lose a reading --------


def test_context_form_failure_does_not_discard_committed_reading(tmp_path):
    db_path = _db(tmp_path)
    result = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-04-01T06:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-04-01T06:00:00-04:00",
        received_at="2026-04-01T10:00:03+00:00",
        delivery_mode="live",
        value=36.4,
    )
    assert result.created is True

    # Simulate the context form failing (e.g. an unknown field slipping
    # through from a buggy caller) -- this must not be able to roll back or
    # otherwise affect the already-committed reading, since the two use
    # entirely separate connections/transactions.
    try:
        upsert_context_entry(db_path, "2026-04-01", not_a_real_field="oops")
        assert False, "expected StorageError"
    except StorageError:
        pass

    reading = get_reading(db_path, result.reading_id)
    assert reading is not None
    assert reading["original_value"] == 36.4


# --- corrections retain prior value + reason ---------------------------------


def test_correct_reading_retains_prior_value_and_reason(tmp_path):
    db_path = _db(tmp_path)
    result = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-05-01T06:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-05-01T06:00:00-04:00",
        received_at="2026-05-01T10:00:02+00:00",
        delivery_mode="live",
        value=36.5,
    )
    reading_id = result.reading_id

    correct_reading(
        db_path, reading_id, new_value=36.9, new_unit="C",
        reason="Misread thermometer display", actor="Alice",
    )

    reading = get_reading(db_path, reading_id)
    assert reading["original_value"] == 36.5
    assert reading["corrected_value"] == 36.9
    assert reading["charting_value"] == 36.9

    # A second correction must retain both prior values in the audit trail.
    correct_reading(
        db_path, reading_id, new_value=37.0, new_unit="C",
        reason="Second look, still off by a bit", actor="Alice",
    )
    history = get_reading_corrections(db_path, reading_id)
    assert len(history) == 2
    assert history[0]["previous_value"] == 36.5
    assert history[0]["new_value"] == 36.9
    assert history[1]["previous_value"] == 36.9
    assert history[1]["new_value"] == 37.0

    reading = get_reading(db_path, reading_id)
    assert reading["charting_value"] == 37.0
    assert reading["original_value"] == 36.5  # immutable evidence, never touched


def test_correct_reading_requires_reason(tmp_path):
    db_path = _db(tmp_path)
    result = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-05-02T06:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-05-02T06:00:00-04:00",
        received_at="2026-05-02T10:00:02+00:00",
        delivery_mode="live",
        value=36.5,
    )
    try:
        correct_reading(
            db_path, result.reading_id, new_value=36.9, new_unit="C", reason="", actor="Alice",
        )
        assert False, "expected StorageError"
    except StorageError:
        pass


def test_correct_reading_unknown_id(tmp_path):
    db_path = _db(tmp_path)
    try:
        correct_reading(db_path, 9999, new_value=36.9, new_unit="C", reason="x", actor="y")
        assert False, "expected StorageError"
    except StorageError:
        pass


# --- exclusion + reversal history --------------------------------------------


def test_exclusion_and_reversal_history(tmp_path):
    db_path = _db(tmp_path)
    result = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-06-01T06:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-06-01T06:00:00-04:00",
        received_at="2026-06-01T10:00:01+00:00",
        delivery_mode="live",
        value=38.9,
    )
    reading_id = result.reading_id

    exclude_reading(
        db_path, reading_id, reason="Fever, not representative of baseline", actor="Bob"
    )
    reading = get_reading(db_path, reading_id)
    assert reading["is_excluded"] == 1

    reverse_reading_exclusion(
        db_path, reading_id, reason="Actually want it visible for context", actor="Bob"
    )
    reading = get_reading(db_path, reading_id)
    assert reading["is_excluded"] == 0

    history = get_reading_exclusion_history(db_path, reading_id)
    assert [event["action"] for event in history] == ["excluded", "reversed"]
    assert history[0]["reason"] == "Fever, not representative of baseline"
    assert history[1]["reason"] == "Actually want it visible for context"


def test_exclude_reading_requires_reason(tmp_path):
    db_path = _db(tmp_path)
    result = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-06-02T06:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-06-02T06:00:00-04:00",
        received_at="2026-06-02T10:00:01+00:00",
        delivery_mode="live",
        value=36.5,
    )
    try:
        exclude_reading(db_path, result.reading_id, reason="", actor="Bob")
        assert False, "expected StorageError"
    except StorageError:
        pass


def test_list_readings_can_omit_excluded(tmp_path):
    db_path = _db(tmp_path)
    kept = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-06-03T06:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-06-03T06:00:00-04:00",
        received_at="2026-06-03T10:00:01+00:00",
        delivery_mode="live",
        value=36.5,
    )
    excluded = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-06-04T06:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-06-04T06:00:00-04:00",
        received_at="2026-06-04T10:00:01+00:00",
        delivery_mode="live",
        value=39.1,
    )
    exclude_reading(db_path, excluded.reading_id, reason="Fever", actor="Bob")

    all_readings = list_readings(db_path)
    assert len(all_readings) == 2

    kept_only = list_readings(db_path, include_excluded=False)
    assert len(kept_only) == 1
    assert kept_only[0]["id"] == kept.reading_id


# --- assignment ---------------------------------------------------------------


def test_assignment_is_revisable_and_retains_history(tmp_path):
    db_path = _db(tmp_path)
    result = record_ble_reading(
        db_path,
        ble_address=ADDRESS,
        device_taken_at_raw="2026-07-01T06:00:00",
        device_taken_at_tz_assumption="America/New_York",
        taken_at="2026-07-01T06:00:00-04:00",
        received_at="2026-07-01T10:00:01+00:00",
        delivery_mode="live",
        value=36.5,
    )
    reading_id = result.reading_id

    reading = get_reading(db_path, reading_id)
    assert reading["assigned_profile"] is None  # unassigned is a valid state

    assign_reading(db_path, reading_id, profile_ref="profile-alice", actor="health-hub")
    reading = get_reading(db_path, reading_id)
    assert reading["assigned_profile"] == "profile-alice"

    # Reassignment doesn't destroy the earlier audit trail.
    assign_reading(db_path, reading_id, profile_ref="profile-bob", actor="health-hub")
    reading = get_reading(db_path, reading_id)
    assert reading["assigned_profile"] == "profile-bob"

    history = get_reading_assignment_history(db_path, reading_id)
    assert [event["profile_ref"] for event in history] == ["profile-alice", "profile-bob"]


def test_assign_reading_unknown_id(tmp_path):
    db_path = _db(tmp_path)
    try:
        assign_reading(db_path, 9999, profile_ref="profile-alice", actor="health-hub")
        assert False, "expected StorageError"
    except StorageError:
        pass


# --- manual entries -----------------------------------------------------------


def test_record_manual_reading(tmp_path):
    db_path = _db(tmp_path)
    reading_id = record_manual_reading(
        db_path,
        taken_at="2026-08-01T06:15:00-04:00",
        received_at="2026-08-01T10:15:03+00:00",
        value=36.6,
        unit="C",
        actor="Alice",
    )
    reading = get_reading(db_path, reading_id)
    assert reading["source_type"] == "manual"
    assert reading["device_id"] is None
    assert reading["original_value"] == 36.6


def test_manual_readings_are_never_deduped(tmp_path):
    db_path = _db(tmp_path)
    kwargs = dict(
        taken_at="2026-08-02T06:15:00-04:00",
        received_at="2026-08-02T10:15:03+00:00",
        value=36.6,
        unit="C",
        actor="Alice",
    )
    first_id = record_manual_reading(db_path, **kwargs)
    second_id = record_manual_reading(db_path, **kwargs)
    assert first_id != second_id
    assert len(list_readings(db_path)) == 2


# --- context entries -----------------------------------------------------------


def test_context_entry_partial_autosave(tmp_path):
    db_path = _db(tmp_path)
    upsert_context_entry(db_path, "2026-09-01", stress=1)
    entry = get_context_entry(db_path, "2026-09-01")
    assert entry["stress"] == 1
    assert entry["illness_fever"] is None  # not entered yet

    upsert_context_entry(db_path, "2026-09-01", illness_fever=0)
    entry = get_context_entry(db_path, "2026-09-01")
    # Earlier field must survive an unrelated partial update.
    assert entry["stress"] == 1
    assert entry["illness_fever"] == 0


def test_context_entry_unknown_field_rejected(tmp_path):
    db_path = _db(tmp_path)
    try:
        upsert_context_entry(db_path, "2026-09-02", bogus_field="x")
        assert False, "expected StorageError"
    except StorageError:
        pass


def test_context_entry_none_vs_unknown_vs_not_entered(tmp_path):
    db_path = _db(tmp_path)
    upsert_context_entry(db_path, "2026-09-03", menstrual_flow="none")
    entry = get_context_entry(db_path, "2026-09-03")
    assert entry["menstrual_flow"] == "none"

    upsert_context_entry(db_path, "2026-09-04", lh_test_result="unknown")
    entry = get_context_entry(db_path, "2026-09-04")
    assert entry["lh_test_result"] == "unknown"

    # A field never touched at all is the distinct "not entered" state.
    entry = get_context_entry(db_path, "2026-09-03")
    assert entry["pregnancy_test_result"] is None


def test_list_context_entries_date_range(tmp_path):
    db_path = _db(tmp_path)
    upsert_context_entry(db_path, "2026-10-01", notes="a")
    upsert_context_entry(db_path, "2026-10-05", notes="b")
    upsert_context_entry(db_path, "2026-10-10", notes="c")

    entries = list_context_entries(db_path, start_date="2026-10-02", end_date="2026-10-09")
    assert [e["entry_date"] for e in entries] == ["2026-10-05"]


def test_context_entry_exclusion_and_reversal_history(tmp_path):
    db_path = _db(tmp_path)
    upsert_context_entry(db_path, "2026-11-01", notes="odd day")

    exclude_context_entry(db_path, "2026-11-01", reason="Data entry mistake", actor="Alice")
    entry = get_context_entry(db_path, "2026-11-01")
    assert entry["is_excluded"] == 1

    reverse_context_entry_exclusion(
        db_path, "2026-11-01", reason="Was fine after all", actor="Alice"
    )
    entry = get_context_entry(db_path, "2026-11-01")
    assert entry["is_excluded"] == 0

    history = get_context_entry_exclusion_history(db_path, "2026-11-01")
    assert [event["action"] for event in history] == ["excluded", "reversed"]


def test_exclude_context_entry_unknown_date(tmp_path):
    db_path = _db(tmp_path)
    try:
        exclude_context_entry(db_path, "2099-01-01", reason="x", actor="y")
        assert False, "expected StorageError"
    except StorageError:
        pass
