# Health Hub Basal Body Temperature Daemon Addendum

Status: design handoff  
Date: 2026-08-19  
Scope: an authoritative basal body temperature (BBT) daemon integrated with Health Hub

## 1. Purpose

This addendum consolidates the agreed requirements for a Health Hub BBT daemon. It is a technical design input, not an implementation specification. Claude Code may refine the schema, API shapes, deployment details, and code organization while preserving the architectural boundaries and product requirements stated here.

The daemon collects readings from supported Bluetooth Low Energy (BLE) basal thermometers, accepts BBT-specific information the thermometer cannot record, stores the complete domain history, renders read-only BBT dashboards, and generates authoritative PDF reports. Health Hub coordinates the person-facing workflow, navigation, viewing, and downloads without becoming a second source of BBT truth.

## 2. Governing architecture

### 2.1 Source of truth

The BBT daemon is the authoritative source for its domain. It owns:

- Raw BLE messages and parsed device readings.
- Normalized temperature observations.
- Device and measurement provenance.
- Health Hub-coordinated patient assignments after they are accepted by the daemon.
- Manually entered temperatures.
- User-entered cycle and measurement context.
- Corrections, exclusions, and their audit history.
- Interpretation-method selection and versioned derived results.
- BBT charts and read-only dashboards.
- PDF construction, storage, revision history, and download content.

Health Hub must not independently recalculate BBT results or generate substitute BBT PDFs. Any Hub cache is operational and disposable; it must be refreshable from the daemon.

### 2.2 Health Hub responsibility

Health Hub coordinates:

- Daemon discovery and availability.
- Authentication, authorization, and the surrounding browser experience.
- The person-selection and assignment workflow.
- Sending assignment actions to the daemon.
- Navigation to the daemon's BBT context-entry interface.
- Presentation or embedding of daemon-produced, non-editing dashboards.
- Initiation, browser viewing, and downloading of daemon-produced PDFs.
- Display of report-generation state.

The Hub interface may ask which person a thermometer or reading belongs to, but the resulting assignment must be persisted by the daemon with the authoritative record.

### 2.3 Conceptual flow

```text
Basal thermometer
       |
       | BLE live/history data
       v
BBT daemon collector -----> authoritative SQLite store
       |                              ^
       |                              |
       +--> BBT context-entry UI -----+
       |
       +--> interpretation engines
       |
       +--> read-only dashboards
       |
       +--> immutable PDF revisions
                   ^
                   |
Health Hub --------+
  assignment coordination, navigation, viewing, downloads
```

## 3. Open-source starting points

### 3.1 BLE implementation

The primary device-library candidate is [`chmielowiec/easyathome-ble`](https://github.com/chmielowiec/easyathome-ble), an MIT-licensed Python package supporting the Easy@Home EBT-300 basal body thermometer. Its published capabilities include:

- Active BLE connection management.
- Automatic device-time synchronization.
- Celsius/Fahrenheit configuration.
- Live temperature notifications.
- Historical-data support.
- Timestamped measurements.

Published EBT-300 protocol details include:

- Service UUID: `0000ffe0-0000-1000-8000-00805f9b34fb`.
- Write characteristic: `0000ffe1-0000-1000-8000-00805f9b34fb`.
- Notify characteristic: `0000ffe2-0000-1000-8000-00805f9b34fb`.
- Temperature in notification bytes 4-5, little-endian, divided by 100.
- Message type differentiating live and historical readings.
- Timestamp fields carried in the notification.

The library is an alpha-stage, single-model starting point. Exact EBT-300 hardware revisions, historical-download completion behavior, reconnect behavior, duplicate delivery, timestamp semantics, and unit behavior require verification. Unit tests or upstream claims do not replace hardware-in-the-loop validation.

### 3.2 Manual-entry and charting reference

[`loreguerra/bbt-chart`](https://github.com/loreguerra/bbt-chart) is useful as a workflow reference for entering BBT, cycle-day, and LH information and plotting it. It is not the proposed daemon foundation because it is a narrow command-line application built around local PostgreSQL, old Plotly integration, separate edit/delete scripts, and a BBT-specific legacy schema.

Useful concepts to retain are:

- Daily temperature entry.
- Cycle-day and LH markers.
- Historical editing and deletion workflows.
- A chart organized by measurement date.

The daemon should implement these concepts within the authoritative architecture described here rather than adopting the legacy application unchanged.

## 4. Collection and persistence requirements

### 4.1 Persistence order

A parsed thermometer reading must be committed to the authoritative store before optional user context is requested. Failure to complete a form must never discard the device observation.

### 4.2 Stable identity and deduplication

Each imported observation requires a stable daemon-owned identifier. Historical synchronization must be idempotent so reconnects and complete-history downloads do not create duplicate readings.

The identity strategy should consider all available source attributes, such as:

- Stable device identifier or daemon-maintained device record.
- Device-provided measurement timestamp.
- Raw notification or a canonical raw-message digest.
- Message type and source stream.
- Device sequence information, when available.

BLE address alone must not be assumed to be a permanent device identity.

### 4.3 Required time fields

Use the established Health Hub time convention:

- `taken_at`: when the person/device actually took the measurement.
- `received_at`: when the daemon received the measurement.

These timestamps are not interchangeable and may differ substantially. A thermometer can retain a reading and deliver it hours or days later during historical synchronization. Sorting clinical history, assigning a reading to a cycle day, and charting must use `taken_at`; transport latency, synchronization monitoring, and ingestion operations use `received_at`.

Preserve separately:

- `device_taken_at_raw`: the device-reported local measurement timestamp before timezone interpretation or correction.
- `taken_at`: the resolved measurement instant, including its timezone/offset when known.
- The timezone or timezone assumption applied to that time.
- `received_at`: the daemon receipt time in UTC.
- `imported_at`, only when a distinct later database-import step exists; it must not replace `received_at`.
- User correction time.
- Report generation time.

The daemon must handle daylight-saving transitions, travel, time-zone changes, naive device timestamps, clock correction, and late historical imports without silently rewriting `device_taken_at_raw`. A later timestamp correction produces a revision to the resolved `taken_at` and retains the prior value and reason.

Avoid using an ambiguous field such as `recorded_at` for either concept. If retained for compatibility, its precise meaning must be documented and mapped explicitly to `taken_at` or `received_at`.

### 4.4 Observation forms

The daemon must support:

- BLE-imported readings.
- Manually entered readings when BLE is unavailable.
- Original device value.
- Corrected value, when a correction is recorded.
- Value used for chart interpretation.
- `taken_at` and `received_at` as distinct timestamps.
- Unit as received or entered.
- Canonical value used internally.
- Measurement method or body site.
- Device identity and source type.
- Live versus historical delivery.
- Raw provenance sufficient for audit and troubleshooting.

### 4.5 Assignment

Health Hub presents the person-assignment workflow. The daemon accepts the assignment action and persists the authoritative association. Until assignment occurs, readings remain safely stored as unassigned. The daemon must support later reassignment or correction without destroying the earlier audit trail.

## 5. BBT context-entry interface

The thermometer supplies only part of a meaningful BBT record. The daemon therefore requires its own editing interface for information outside the thermometer's capabilities.

### 5.1 Context fields

Support, as applicable:

- Menstrual flow and cycle-start information.
- Cycle day, preferably derived from an authoritative cycle start while remaining inspectable.
- LH-test result.
- Pregnancy-test result.
- Cervical-mucus observation.
- Cervical observation when required by a selected method.
- Measurement method or body site.
- Sleep duration and interrupted-sleep status.
- Deviation from the usual measurement time.
- Illness or fever.
- Stress.
- Shift work.
- Alcohol.
- Travel or time-zone change.
- Medication that may affect interpretation.
- Other disturbance flags.
- Free-text notes.
- Explicit exclusion and exclusion reason.
- Manual correction and correction reason.

These factors are pertinent because illness, fever, stress, shift work, interrupted sleep, oversleeping, alcohol, travel, some medications, and inconsistent measurement conditions can affect BBT interpretation.

### 5.2 Interaction design

The primary daily action should be a compact, phone-friendly "complete today's entry" form. Requirements include:

- Large touch targets and quick-select controls.
- Optional fields rather than forced guesses.
- Distinct values for `unknown`, `none`, and `not entered`.
- Partial-entry autosave.
- Clear presentation of the already-saved thermometer value.
- A required reason for a correction or exclusion.
- A distinction between `disturbed` and `excluded`.

A disturbed reading remains visible and is not automatically excluded. The selected interpretation method may treat it according to its documented rules.

### 5.3 Editing boundary

Dashboards shown through Health Hub are read-only. Editing occurs through the daemon's dedicated context-entry interface. Health Hub may link or route the user to that interface but does not directly modify its own competing BBT record.

## 6. Corrections, exclusions, and audit

Original readings are immutable evidence. Corrections and exclusions must be append-only domain events or otherwise retain equivalent history.

For a corrected reading, retain:

- Original value and unit.
- Corrected value and unit.
- Value selected for current charting.
- Correction timestamp.
- Reason.
- Actor or trusted origin of the action.

For an exclusion, retain:

- Original observation.
- Exclusion state.
- Reason.
- Timestamp.
- Actor or origin.
- Reversal history, if the exclusion is later removed.

Reports and dashboards must visibly distinguish imported, manual, corrected, disturbed, and excluded observations.

## 7. Interpretation modes

Raw observations remain method-neutral. Interpretation is a derived, versioned view. The daemon supports four modes:

1. Chart only -- no interpretation.
2. Sensiplan.
3. SymptoPro.
4. Taking Charge of Your Fertility (TCOYF).

### 7.1 Chart-only mode

Chart-only mode displays all recorded observations and context without calculating:

- A coverline.
- A temperature shift.
- Fertile-window opening or closing.
- Ovulation day.
- Fertile or infertile status.

This is the default-safe mode when the user has not deliberately selected an interpretation method or prefers interpretation with a clinician or trained instructor.

### 7.2 Method isolation

Each named method must be implemented as an independent engine. The daemon must never create a hybrid from the methods' temperature, mucus, calendar, coverline, or fertile-window rules.

Every derived result must identify:

- Method name.
- Implemented method/rule version.
- Input observation revision set.
- Calculation time.
- Detected shift, coverline, or window, when applicable.
- Observations that affected the result.
- `insufficient data` when the rule requirements are unmet.

Changing methods recalculates derived interpretation without changing source observations.

### 7.3 Method sources and validation

Exact rules must come from authorized, versioned method documentation rather than blogs, summaries, or inferred behavior. Naming, teaching content, chart formats, trademarks, and licensing must be reviewed before distribution. Each engine requires golden reference charts that demonstrate agreement with its authoritative examples.

Sensiplan is the highest-priority method because published reviews identify it as the symptothermal method with the strongest evidence base. SymptoPro and TCOYF are included as distinct established charting approaches; their rules must not be treated as interchangeable with Sensiplan.

### 7.4 Interpretation language

The UI must communicate uncertainty. Prefer phrases such as:

- "Temperature shift detected."
- "Ovulation may have occurred."
- "Insufficient data."
- "Interpretation affected by disturbed readings."

Do not imply that BBT alone reliably predicts ovulation. Temperature changes generally provide retrospective evidence and can be affected by fever or measurement conditions.

## 8. Dashboards and graphs

The daemon owns all BBT-specific graph construction. Health Hub coordinates browser presentation of the daemon's read-only output.

### 8.1 Primary single-cycle chart

The primary chart uses:

- Cycle day on the horizontal axis, with calendar date available.
- Temperature in the selected unit on the vertical axis.
- One point per daily reading.
- Straight connections between consecutive valid readings.
- Gaps for missing days; no interpolation.
- Distinct markers for disturbed readings.
- Visible but unconnected excluded readings.
- Aligned context tracks below the temperature plot.
- Exact values and context in accessible details or hover content.

Do not smooth the curve by default. Smoothing can make uncertain patterns appear conclusive.

### 8.2 Symptothermal chart

The symptothermal view aligns tracks on a shared cycle-day axis:

```text
Temperature       --o--o--O--o--o--
Menstrual flow    H  H  M  L  -  -
Cervical mucus    D  D  C  W  E  E
LH test           -  -  -  +  +  -
Disturbances      -  -  S  -  -  I
Cycle day         1  2  3  4  5  6
```

Symbols are illustrative; the implementation must provide an explicit legend and accessible labels rather than relying on color alone.

### 8.3 Multi-cycle comparison

Provide a historical comparison that stacks cycles or overlays them by cycle day. Requirements:

- Current cycle emphasized.
- Older cycles visually muted.
- Individual cycles selectable.
- Consistent scale for valid comparisons.
- Different measurement methods or device types clearly identified.
- No silent averaging.

Skin/wearable temperature must not be silently merged with oral or other basal thermometer data because different measurement technologies may have different ranges and may not agree on inferred cycle events.

### 8.4 Supporting views

Useful supporting views include:

- Measurement time by cycle day, including deviation from the user's usual time.
- Daily detail table.
- Cycle summary with length, coverage, missing days, disturbances, exclusions, LH-positive day, device, and selected method.
- Multiple-cycle history for recurring patterns.

### 8.5 Method overlays

A coverline, shift marker, estimated ovulation marker, or fertile-window shading may appear only when a named method is selected and the overlay is produced by that method's versioned engine. The dashboard and PDF must display the method and version.

## 9. PDF reporting

The daemon is the sole arbiter of BBT PDF generation. Health Hub may request, display, embed, and download daemon-produced reports but must not reconstruct their clinical/domain content.

### 9.1 Report types

At minimum, support:

- Personal chart: visual, concise, and understandable.
- Detailed/clinician report: chart, daily table, context, disturbances, corrections, exclusions, units, and provenance summary.

### 9.2 Suggested structure

Page 1:

- Report title and covered date/cycle range.
- Person assigned through the authoritative daemon record.
- Generation timestamp and timezone.
- Selected interpretation method and version, or chart-only status.
- Single-cycle or selected-period BBT chart.
- Aligned menstrual, mucus, LH, disturbance, and exclusion markers.
- Concise cycle summary.

Subsequent pages:

- Daily detail table.
- Notes and disturbances.
- Correction and exclusion summary.
- Measurement method, device, and provenance summary.
- Technical appendix only when requested.

### 9.3 Immutability and reproducibility

Each generated PDF is an immutable revision. If underlying observations, context, assignment, exclusions, or interpretation-engine versions change, generate a new report revision rather than silently replacing the old document.

Persist enough information to reproduce or explain a report:

- Report identifier and revision.
- Covered observation IDs and revisions.
- Selected method and engine version.
- Generation parameters.
- Generation time and timezone.
- Content digest.
- File location or durable blob reference.
- Superseding report identifier, when applicable.

Dashboard and PDF projections must use the same reporting/query layer so their results do not disagree.

## 10. Hub-facing capabilities

The exact API is an implementation decision, but the daemon must expose discoverable capabilities for:

- Supported devices.
- Assignment actions.
- BBT context-entry location.
- Available read-only dashboards and date ranges.
- Supported interpretation modes.
- Available report types.
- Report-generation initiation.
- Report status.
- Browser viewing.
- PDF download.

Report status should be explicit, for example:

- `pending`
- `ready`
- `failed`
- `superseded`

Stable identifiers are required for devices, readings, dashboards/views, report jobs, and completed report revisions. Health Hub should treat daemon output as authoritative and its own cached metadata as replaceable.

## 11. Interoperability

The internal schema is daemon-owned. If clinical data export is added, body temperature can be represented using established health-data terminology and transport conventions, including:

- FHIR `Observation` for body-temperature measurements.
- Appropriate LOINC body-temperature coding selected for the actual measurement context.
- UCUM temperature units, such as Celsius and degrees Fahrenheit.

Terminology mapping must not erase BBT context, measurement method, device provenance, corrections, or the distinction between raw observation and derived fertility interpretation.

## 12. Testing and acceptance evidence

### 12.1 BLE and persistence

- Parse captured live and historical EBT-300 notifications.
- Exercise connection, disconnect, reconnect, and partial-sync behavior.
- Confirm repeated history downloads are idempotent.
- Confirm a context-form failure cannot lose an imported reading.
- Validate timestamps, units, and raw provenance.
- Run hardware-in-the-loop tests on each claimed thermometer model/revision.

### 12.2 Time and data quality

- Naive device times.
- Daylight-saving transitions.
- Travel and timezone changes.
- Device clock correction.
- Late historical import.
- A reading whose `received_at` is days after its `taken_at`.
- Missing days.
- Multiple readings on one day.
- Manual and BLE readings on the same day.
- Fever/disturbed readings.
- Corrections and reversed exclusions.
- Cycles without a detectable shift.

### 12.3 Interpretation

- Separate golden reference charts for Sensiplan, SymptoPro, and TCOYF.
- Exact method/version in every result.
- `insufficient data` paths.
- No cross-method rule leakage.
- Recalculation after context correction without source-data mutation.
- Preservation of earlier derived results and reports after an engine upgrade.

### 12.4 Presentation and PDF

- Missing readings remain gaps.
- Disturbed and excluded points are distinguishable without color alone.
- Dashboard and PDF agree for identical inputs and engine versions.
- Old PDF revisions remain unchanged after later corrections.
- A completed report can be reproduced or fully explained from retained inputs.
- Health Hub can view and download the daemon-produced file without regenerating it.

## 13. Safety and presentation constraints

- Do not label an estimated ovulation day as a confirmed clinical fact.
- Do not claim that BBT alone predicts the fertile window reliably.
- Do not silently mix measurement sites or device technologies.
- Do not hide disturbed, excluded, or corrected observations.
- Do not replace original device evidence with user edits.
- Do not treat unit tests as proof of hardware support.
- Do not let Health Hub become a competing BBT database or report generator.
- Clearly state when a report is chart-only versus method-interpreted.

## 14. Implementation priorities

Recommended sequence, without prescribing code organization:

1. Verify `easyathome-ble` behavior against an EBT-300 and capture fixtures.
2. Establish authoritative SQLite persistence, stable IDs, raw provenance, and deduplication.
3. Add Health Hub-coordinated assignment persistence.
4. Add the compact daemon-owned BBT context-entry interface.
5. Implement chart-only single-cycle and symptothermal dashboards.
6. Add immutable PDF generation from the shared reporting layer.
7. Implement and validate one versioned interpretation method at a time, beginning with Sensiplan.
8. Add multi-cycle comparison and supporting views.
9. Complete Hub discovery, viewing, download, and status coordination.
10. Claim device or method support only after the corresponding acceptance evidence exists.

## 15. Reference material

- Easy@Home BLE implementation: <https://github.com/chmielowiec/easyathome-ble>
- Easy@Home BLE package information: <https://pypi.org/project/easyathome-ble/>
- BBT Chart reference application: <https://github.com/loreguerra/bbt-chart>
- ACOG fertility-awareness overview: <https://www.acog.org/womens-health/faqs/fertility-awareness-based-methods-of-family-planning>
- Mayo Clinic BBT measurement factors: <https://www.mayoclinic.org/tests-procedures/basal-body-temperature/about/pac-20393026>
- Planned Parenthood temperature-method overview: <https://www.plannedparenthood.org/learn/birth-control/fertility-awareness/whats-temperature-method-fams>
- Sensiplan official FAQ: <https://www.sensiplan.de/en/faqs>
- SymptoPro: <https://symptopro.org/>
- TCOYF downloadable charts: <https://www.tcoyf.com/downloadable-charts/>
- Fertility-awareness methods research overview: <https://pmc.ncbi.nlm.nih.gov/articles/PMC9171018/>
- Comparative wearable/oral-temperature study: <https://pmc.ncbi.nlm.nih.gov/articles/PMC4704931/>
- US Core Body Temperature profile: <https://build.fhir.org/ig/HL7/US-Core/StructureDefinition-us-core-body-temperature.html>

## 16. Branding assets

The project folder includes three generated branding assets based on the approved Home Health Hub branding guide and existing daemon-banner family:

| File | Intended use |
|---|---|
| `easyathome-bbt-daemon-banner.png` | 3:1 README banner showing basal thermometer, Bluetooth, home gateway, and local storage |
| `bbt-daemon-icon.png` | Full-color transparent daemon/application icon combining thermometer, temperature trend, and cycle ring |
| `bbt-daemon-nav-icon.png` | Simplified deep-teal transparent navigation icon for small UI sizes |

The banner uses `easyathome-bbt-daemon` as the working repository name. If the final repository name differs, regenerate or edit the banner title before publishing; the branding guide requires the exact repository name to appear once.

The assets use the shared teal/aqua visual family and approved cycle plum `#8E5AA7` for BBT-specific chart and cycle details. Manufacturer logos, cloud imagery, people, pregnancy imagery, medical claims, and arbitrary temperature values are intentionally absent.

## 17. Final architectural statement

Health Hub coordinates discovery, person-selection workflows, navigation, browser viewing, and downloads. The BBT daemon owns its domain truth: device collection, authoritative storage, accepted assignments, BBT context, corrections, method-specific interpretation, specialized read-only dashboards, and immutable PDF reports.
