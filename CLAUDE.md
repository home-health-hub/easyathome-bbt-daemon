# Project notes for easyathome-bbt-daemon

## Related repos to watch

- **easyathome-ble** -- https://github.com/chmielowiec/easyathome-ble --
  this daemon's BLE protocol dependency, pulled as a normal versioned PyPI
  dependency (`easyathome-ble>=0.2.4`), not vendored or forked. Alpha-stage
  and single-model per its own README; a future release could change
  `EasyHomeDevice`'s constructor shape, add a `discover()` helper, or
  change `notify_callback` from synchronous to async -- any of which would
  need `collector.py` revisited, not just a version bump.
- **health-thermometer-daemon** -- https://github.com/home-health-hub/health-thermometer-daemon
  -- the structural/style template this daemon's project layout,
  `pyproject.toml`/`.flake8`/`.gitignore` conventions, config-file style,
  systemd unit layout, and README structure were deliberately modeled on.
  Not a code dependency -- its BLE protocol code targets a different device
  family entirely (the public Bluetooth SIG Health Thermometer Profile, not
  an Easy@Home-proprietary characteristic layout) and was not reused.

## Why the time model has four fields, not one

`storage.py` keeps `device_taken_at_raw`, `taken_at`,
`device_taken_at_tz_assumption`, and `received_at` (plus optional
`imported_at`) as separate fields rather than a single `recorded_at`
timestamp, per addendum section 4.3. This isn't over-engineering for its
own sake -- each answers a different question that a single field cannot:

- **`device_taken_at_raw`** is what the EBT-300 itself reported: a naive
  year/month/day/hour/minute/second with no UTC offset (`parser.py` in
  `easyathome-ble` confirms the wire format carries no timezone byte at
  all). It is never rewritten, even by a later correction -- it is the
  device's own claim, kept as evidence independent of whatever the daemon
  later decides that claim meant.
- **`taken_at`** is the daemon's *resolved* answer to "when did this
  measurement actually happen," produced by attaching `device_taken_at_raw`
  to `device_taken_at_tz_assumption` (see `resolve_naive_local_time`). This
  is what sorting, cycle-day assignment, and charting must use. It is
  revisable -- a discovered device-clock error, a DST misinterpretation, or
  a travel/timezone correction produces a new `taken_at` via
  `correct_reading_time`, which appends to `reading_time_corrections`
  rather than silently overwriting history.
- **`received_at`** is when *this daemon's process* received the
  notification, always in UTC, always daemon-clock-based (never derived
  from anything the device claims). The EBT-300 can hand over a batch of
  historical readings at connect time -- possibly hours or days after they
  were actually taken -- and `received_at` is what makes that gap visible
  and auditable (`test_late_historical_import_received_days_after_taken`
  in `tests/test_storage.py` exercises exactly this).
- **`imported_at`** exists only for a genuinely distinct later
  database-import step (e.g. a future bulk-import tool reading an export
  file) and must never be set to the same value as `received_at` just
  because both happened to run in the same code path -- see
  `record_ble_reading`'s docstring. Nothing in this release's collection
  path (`collector.py`) sets it.

Collapsing any two of these into one field loses a real distinction: a
`recorded_at` that conflated `taken_at` and `received_at` would silently
break cycle-day math the moment a historical sync arrived late, which for
this device class (batch history flush) is an expected, not exceptional,
event.

## Why corrections and exclusions are append-only, not UPDATE-in-place

Addendum section 6 requires that original readings remain immutable
evidence and that corrections/exclusions retain their own history
(original value, corrected value, reason, actor, timestamp; and for
exclusions, reversal history if later undone). `storage.py` implements
this with dedicated audit tables (`reading_corrections`,
`reading_time_corrections`, `reading_exclusions`,
`reading_assignments`, `context_entry_exclusions`) that only ever grow via
`INSERT`, never `UPDATE` or `DELETE`.

The `readings`/`context_entries` tables *do* also carry a cached "current
value" column (`corrected_value`, `charting_value`, `is_excluded`,
`assigned_profile`, ...) that gets updated in place on every correction --
but that's a read-optimization derived from the audit table, not a
replacement for it. The audit table is the source of truth; if the two
ever disagree, the audit table wins and the cache is a bug to fix, not the
other way around. This distinction matters most for a later PDF/report
feature (addendum section 9.3: reports must be reproducible from retained
inputs) -- reproducing an old report needs the audit trail, not just
today's cached value.

## Why the `reports` table is immutable revisions, not cached rows

Addendum section 9.3 requires that "each generated PDF is an immutable
revision" and that regenerating a report from changed inputs produces a
*new* revision rather than silently replacing the old document. `reports`
implements this with a shape that looks superficially like the
correction/exclusion audit tables but is subtly different in what "the
row" means:

- For `reading_corrections`/`reading_exclusions`, the audit table is the
  history and `readings` holds a *cache* of the current value -- the cache
  gets overwritten in place on every correction (see the section above),
  because there's exactly one "current" reading value at a time.
- For `reports`, there is no separate cache/history split. Every row *is*
  a complete, self-contained revision -- its own `generated_at`,
  `content_digest`, `file_path`, and `generation_params` are never
  rewritten once set. "Regenerating a report" doesn't update a row; it
  inserts a new one via `record_report(..., supersedes=<old id>)`, which
  reuses the old row's `report_uid` (the stable identity of the *logical*
  report across revisions) and increments `revision`.

The one thing that *does* get updated in place is `supersede_report`
flipping the old row's `status` to `"superseded"` and setting its
`superseded_by` pointer -- but this only ever touches those two columns,
never the old row's own generation facts. `test_supersede_report_never_touches_old_revisions_own_facts`
in `tests/test_storage.py` asserts exactly this: the old PDF's digest,
file path, and generation timestamp survive supersession untouched, the
same guarantee `correct_reading` gives `readings.original_value`.

Two status transitions happen instead of one insert-and-done, mirroring a
real generation attempt's lifecycle: `record_report` first inserts the row
as `"pending"` (addendum section 10's status vocabulary), then
`update_report_status` flips it to `"ready"` (with digest and file path)
or `"failed"` once `report.py`'s `build_pdf` call actually finishes or
raises. This means a crash mid-render leaves a `"pending"` row behind
rather than either a phantom `"ready"` row with no file, or nothing at
all -- see `generate_report`'s `try`/`except` in `report.py`.

`report_covered_readings` is a join table, not a serialized ID list column,
so "which reports cover reading N" is a normal indexed query rather than a
scan-and-parse -- the addendum leaves this choice open ("a serialized list
or join table -- your call"), and a join table costs nothing extra here
since SQLite handles the extra table for free at this data scale.

## Why the chart's context tracks are plain text rows this phase, not a graphical symptothermal chart

Addendum section 8.2 describes a fuller symptothermal chart with aligned
graphical tracks (mucus symbols, flow intensity bars, etc.) and explicitly
allows the underlying method's overlays (coverline, shift marker) to be
layered on *when a named interpretation method is selected* -- none of
which exists yet (chart-only mode has no method to select, per section
7.1). `chart.py`'s `_add_context_tracks` renders the required tracks
(menstrual flow, cervical mucus, LH test, disturbances, cycle day, plus a
calendar-date row) as plain aligned text/one-letter-code rows with an
explicit, data-driven legend -- satisfying section 8.1's minimum ("aligned
context tracks below the temperature plot") and section 8.2's
accessibility requirement (no color-only encoding), without building the
richer graphical rendering section 8.2 sketches. Building that richer view
is explicitly listed as a later phase in the addendum's section 14
sequencing (chart-only and symptothermal dashboards are the same numbered
step, but nothing here blocks doing the graphical version later against
the same `ChartData`/`build_chart_data` data shape -- only `render_chart`
would need to change).

## Why identity/dedup doesn't use the BLE address alone

Addendum section 4.2 explicitly warns against treating a BLE address as a
permanent device identity. `storage.py` follows that by keying `readings`
on a computed `reading_uid` digest (device address + device's own naive
timestamp + live/historical delivery mode + reported value -- see
`compute_reading_uid`'s docstring for exactly why those four and not the
raw notification bytes) rather than the address directly, and by giving
every physical device its own daemon-owned integer `device_id` row (
`devices` table) that readings reference instead of embedding the address
string on every row. If a future device generation exposes a real serial
number, or an address needs remapping (e.g. the adapter reassigns it),
only the `devices` row needs to change.

Known gap, worth revisiting once real fixtures exist: if the *same*
physical measurement is ever delivered once tagged live and later again
tagged historical (or vice versa) with a timestamp differing by even one
second, the current digest treats them as two distinct readings, since
`easyathome-ble` exposes no correlating sequence number to disambiguate.

## Interpretation-method rule sourcing -- read this before touching Sensiplan/SymptoPro/TCOYF

None of the three named interpretation engines (Sensiplan, SymptoPro,
TCOYF) exist in this release -- see the README's "Current scope" section.
When one of them *is* built, addendum section 7.3 imposes a hard
constraint that is easy to violate by accident: **exact rules must come
from authorized, versioned method documentation, never from blogs,
summaries, or inferred/reverse-engineered behavior.** Naming, teaching
content, chart formats, trademarks, and licensing must be reviewed before
distribution, and each engine needs golden reference charts demonstrating
agreement with its authoritative source. This is a legal and clinical-
safety constraint, not a style preference -- a plausible-looking Sensiplan
rule copied from a summary article is exactly the failure mode this
section exists to prevent. Don't free-hand these rules; find and cite the
authoritative versioned source first.

## Open questions (unverified against real hardware)

Nothing below has been exercised against a physical EBT-300. All of it is
inference from `easyathome-ble` v0.2.4's published source and README.

- **Connection lifecycle.** `cli.py`'s `run_daemon` reconnects for every
  collection attempt (connect -> collect for a fixed window -> disconnect
  -> retry) rather than holding one persistent connection, mirroring
  `health-thermometer-daemon`'s reasoning: whether the device stays
  connectable indefinitely or only briefly is unconfirmed, and this shape
  works under either assumption. See `collector.collect_once`'s docstring.
- **No "history download complete" signal.** The EBT-300 can apparently
  flush a batch of historical readings on connect (message type 17 vs. 1
  in `parser.py`), but the library gives no event for "the batch is done."
  `collect_once` just stays connected for a fixed window
  (`DEFAULT_WINDOW_SECONDS`, currently a guess, not a tuned value) and
  takes whatever arrives, relying on dedup for safety if a batch is
  redelivered on a later attempt that overlaps the window differently.
- **Automatic device clock sync on every connect.** `EasyHomeDevice.connect()`
  (in `easyathome-ble`) unconditionally sends a time-sync command using
  the *host machine's* local time on every single connection -- not just
  first pairing, and not configurable in this daemon. This means
  `config.device_timezone` should describe the same timezone the host
  itself runs in; if they disagree, the device's actual clock and this
  daemon's assumption for resolving `device_taken_at_raw` would silently
  disagree too. Confirm this is actually desirable once real hardware is
  available (e.g. does it matter if the host machine is NTP-synced but in
  the wrong configured timezone?).
- **Discovery reliability.** There is no `discover()` in `easyathome-ble`
  (confirmed from source, not assumed) -- `collector.collect_once` scans
  for the configured address via `bleak.BleakScanner.find_device_by_address`.
  Whether the EBT-300 reliably advertises in a way that scan can find is
  unverified.
- **Write-command behavior.** `EasyHomeDevice._write_command`'s
  response/no-response retry logic (time sync, unit sync) has never been
  exercised against a real GATT server's actual characteristic properties.

## Verification status

See the README's warning banners for current hardware-verification
status. Nothing in this daemon -- or its `easyathome-ble` dependency --
has been run against a real thermometer yet. Do not remove either warning
banner until that has actually happened.
