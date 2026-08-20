"""BLE collector: bridges ``easyathome-ble`` to ``storage.py``.

UNVERIFIED AGAINST REAL HARDWARE. This module is written strictly from
``easyathome-ble`` v0.2.4's published source and README -- see
``docs/HEALTH_HUB_BBT_DAEMON_ADDENDUM.md`` section 3.1 and this package's
``CLAUDE.md`` for what "unverified" means here and what to check first once
a real EBT-300 is available.

Two things about the library's actual API surface (confirmed by reading
``easyathome_ble/device.py``, not guessed) shape this module:

- ``EasyHomeDevice`` exposes no ``discover()`` of its own -- unlike some
  sibling daemons' driver libraries, it expects the caller to already have
  a resolved ``bleak.backends.device.BLEDevice`` before constructing it.
  This collector performs that scan itself via
  ``BleakScanner.find_device_by_address()``.
- ``notify_callback`` is a *synchronous* ``Callable[[TemperatureMeasurement],
  None]``, not a coroutine. Persisting a reading therefore happens directly
  inside that callback via ``storage.py``'s synchronous ``sqlite3`` calls --
  there is no async bridging to get wrong.

A behavior worth flagging for anyone extending this module: ``EasyHomeDevice.
connect()`` unconditionally sends a time-sync command using the *host
machine's* local time (``datetime.now().astimezone()``) on every single
connection, not just on first pairing. This daemon does not opt out of that
(there is no parameter to). Practically, this means ``config.device_timezone``
should describe the same timezone the host machine itself runs in --
otherwise the device's own clock and this daemon's timezone assumption for
interpreting ``device_taken_at_raw`` disagree, and readings could resolve to
the wrong instant. See CLAUDE.md's "Open questions" for more.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from bleak import BleakScanner
from easyathome_ble import EasyHomeDevice, TemperatureMeasurement

from . import storage

_LOGGER = logging.getLogger(__name__)

#: How long to stay connected collecting whatever notifications arrive
#: (a live reading, and/or a batch of historical readings the device
#: flushes on connect) before disconnecting. Not configurable in this
#: phase -- there's no fixture data yet to justify a specific default over
#: any other, so this is a starting guess, not a tuned value.
DEFAULT_WINDOW_SECONDS = 20.0

#: How long to wait for a BLE advertisement from the configured address
#: before giving up on this attempt.
DEFAULT_SCAN_TIMEOUT_SECONDS = 15.0


class CollectorError(Exception):
    """Raised when a collection attempt cannot complete.

    Deliberately a single generic exception type rather than one per
    failure mode (scan timeout, connect failure, etc.) -- the caller
    (``cli.py``'s retry loop) treats every failure the same way: log it and
    retry after ``daemon.retry_seconds``, so a finer-grained hierarchy
    wouldn't currently be used for anything.
    """


def _utc_now_iso() -> str:
    """Return the current instant as a UTC ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _store_measurement(
    db_path: str,
    ble_address: str,
    device_timezone: str,
    measurement: TemperatureMeasurement,
) -> None:
    """Resolve and persist one parsed measurement (addendum 4.1-4.3).

    Called synchronously from ``EasyHomeDevice``'s notification callback.
    Any exception raised here propagates out of that callback into bleak's
    notification dispatch -- deliberately not swallowed, since a storage
    failure losing a reading silently would be worse than a visible crash
    of this collection attempt (the outer retry loop in ``cli.py`` will try
    again).

    Args:
        db_path: Filesystem path to the SQLite database file.
        ble_address: The device's BLE address.
        device_timezone: IANA timezone name used to resolve the device's
            naive ``measurement.timestamp`` into an absolute instant.
        measurement: The parsed measurement from ``easyathome_ble``.
    """
    device_taken_at_raw = measurement.timestamp.isoformat()
    taken_at = storage.resolve_naive_local_time(device_taken_at_raw, device_timezone)
    received_at = _utc_now_iso()
    delivery_mode = "live" if measurement.is_live else "historical"

    result = storage.record_ble_reading(
        db_path,
        ble_address=ble_address,
        device_taken_at_raw=device_taken_at_raw,
        device_taken_at_tz_assumption=device_timezone,
        taken_at=taken_at,
        received_at=received_at,
        delivery_mode=delivery_mode,
        value=measurement.temperature,
        unit="C",  # easyathome-ble always reports Celsius (see models.py)
    )
    _LOGGER.info(
        "%s reading %s (id=%s, created=%s): %.2f C at %s",
        delivery_mode,
        "recorded" if result.created else "already known (dedup)",
        result.reading_id,
        result.created,
        measurement.temperature,
        taken_at,
    )


async def collect_once(
    db_path: str,
    ble_address: str,
    device_timezone: str,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    scan_timeout: float = DEFAULT_SCAN_TIMEOUT_SECONDS,
) -> bool:
    """Connect once, collect whatever notifications arrive, then disconnect.

    Unlike a device that only ever delivers a single reading per
    connection, the EBT-300 can flush a batch of historical readings
    immediately on connect in addition to (or instead of) a live one, per
    the addendum's "message type differentiating live and historical
    readings" note. There is no documented signal for "history download
    complete" in this library, so this function does not try to detect
    one -- it stays connected for a fixed window and takes whatever
    arrives, relying on ``storage.record_ble_reading``'s dedup for safety
    if the same historical batch is redelivered on a later attempt.

    Args:
        db_path: Filesystem path to the SQLite database file.
        ble_address: The device's BLE address, as configured.
        device_timezone: IANA timezone name to resolve naive device
            timestamps under.
        window_seconds: How long to stay connected after a successful
            connect, collecting notifications, before disconnecting.
        scan_timeout: How long to wait for an advertisement from
            ``ble_address`` before giving up on this attempt.

    Returns:
        True if at least one reading was received (whether or not it was
        new -- a dedup hit still confirms the device is reachable).

    Raises:
        CollectorError: If the device isn't found during the scan, or the
            connect attempt fails.
    """
    ble_device = await BleakScanner.find_device_by_address(ble_address, timeout=scan_timeout)
    if ble_device is None:
        raise CollectorError(
            f"No BLE advertisement seen for {ble_address} within {scan_timeout}s"
        )

    received_any = False

    def _on_measurement(measurement: TemperatureMeasurement) -> None:
        nonlocal received_any
        _store_measurement(db_path, ble_address, device_timezone, measurement)
        received_any = True

    device = EasyHomeDevice(ble_address, _on_measurement, ble_device=ble_device)
    try:
        await device.connect()
    except Exception as exc:
        # Deliberately broad: bleak/dbus-fast/bleak_retry_connector can raise
        # all sorts of things beyond BleakError depending on backend and
        # failure mode (see collect_once's docstring on CollectorError), and
        # this is unverified against a real adapter/device pairing.
        raise CollectorError(f"Could not connect to {ble_address}: {exc}") from exc

    try:
        await asyncio.sleep(window_seconds)
    finally:
        await device.disconnect()

    return received_any
