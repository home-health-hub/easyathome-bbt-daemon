"""Command-line entry point and daemon run loop for BLE collection.

UNVERIFIED AGAINST REAL HARDWARE -- see the README warning banner and
CLAUDE.md's "Open questions". This loop reconnects for every collection
attempt (connect -> collect whatever notifications arrive within a fixed
window -> disconnect -> retry after ``daemon.retry_seconds``) rather than
holding one persistent connection open, for the same reason sibling daemon
health-thermometer-daemon's ``cli.py`` does: whether the EBT-300 stays
connectable indefinitely or is only briefly connectable around a
measurement/history-sync event is unconfirmed, and this shape works
correctly under either assumption -- see ``collector.collect_once``'s
docstring for the EBT-300-specific reasoning (a history batch, unlike a
single Health Thermometer Profile reading, has no known "done" signal).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from ._version import __version__
from .collector import DEFAULT_WINDOW_SECONDS, CollectorError, collect_once
from .config import ConfigError, DaemonConfig, load_config
from .storage import ensure_schema

_LOGGER = logging.getLogger("easyathome_bbt_daemon")

#: Exceptions worth retrying rather than crashing the daemon on. OSError
#: covers D-Bus/socket-level failures below bleak's own exception types;
#: asyncio.TimeoutError covers a hung scan/connect. CollectorError wraps
#: everything collect_once itself decided was retryable -- see its
#: docstring.
_RETRYABLE_ERRORS = (CollectorError, OSError, asyncio.TimeoutError)


async def _sleep_or_stop(seconds: float, stop_event: asyncio.Event) -> None:
    """Sleep for ``seconds``, waking early if ``stop_event`` is set.

    Used for the retry backoff between connection attempts, so a stop
    signal received mid-wait doesn't have to wait out the full interval
    before the daemon actually exits.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_daemon(
    config: DaemonConfig, once: bool = False, once_timeout: float = 60.0
) -> bool:
    """Run the connect/collect/disconnect/retry loop against ``config.address``.

    Args:
        config: Loaded daemon configuration (``daemon.address`` is
            required -- see ``config.load_config``; there is no
            auto-discovery path, since ``easyathome-ble`` exposes none).
        once: If True, make exactly one collection attempt and exit
            afterward (whether or not it received anything), instead of
            retrying indefinitely -- for an on-demand capture run by hand.
        once_timeout: Seconds to use as the collection window (and scan
            timeout) in ``--once`` mode, instead of
            ``collector.DEFAULT_WINDOW_SECONDS``.

    Returns:
        True if at least one reading was received during the run.
    """
    ensure_schema(config.db_path)
    stop_event = asyncio.Event()
    reading_received = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    _LOGGER.info(
        "Starting easyathome-bbt-daemon %s for device %s%s",
        __version__,
        config.address,
        f" (once, {once_timeout}s window)" if once else "",
    )

    window_seconds = float(once_timeout) if once else DEFAULT_WINDOW_SECONDS

    while not stop_event.is_set():
        try:
            got_any = await collect_once(
                config.db_path,
                config.address,
                config.device_timezone,
                window_seconds=window_seconds,
                scan_timeout=window_seconds,
            )
        except _RETRYABLE_ERRORS as exc:
            _LOGGER.warning("Collection attempt failed: %s", exc)
            if once:
                break
            _LOGGER.info("Retrying in %ss", config.retry_seconds)
            await _sleep_or_stop(config.retry_seconds, stop_event)
            continue

        if got_any:
            reading_received = True
            _LOGGER.info("Collection attempt completed with at least one reading")
        else:
            _LOGGER.debug("Collection attempt completed with no readings")

        if once:
            break

        await _sleep_or_stop(config.retry_seconds, stop_event)

    return reading_received


def _check_config(config_path: str) -> int:
    """Validate the config file and print a summary, without running.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        0 if valid, 1 otherwise (each error is printed).
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"{config_path}: INVALID")
        print(f"  - {exc}")
        return 1

    print(f"{config_path}: OK")
    print(f"  daemon: address={config.address} retry_seconds={config.retry_seconds}")
    print(f"  daemon: device_timezone={config.device_timezone} log_level={config.log_level}")
    print(f"  storage: db_path={config.db_path}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="easyathome-bbt-daemon",
        description=(
            "Standalone BLE daemon that collects Easy@Home EBT-300 basal body "
            "temperature readings and stores them locally. UNVERIFIED against "
            "real hardware -- see README."
        ),
    )
    parser.add_argument(
        "-c", "--config", required=True, help="Path to the daemon's INI configuration file"
    )
    parser.add_argument(
        "-k",
        "--check-config",
        action="store_true",
        help="Validate the config file and exit, without starting the daemon",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging (overrides the config file's log level)",
    )
    parser.add_argument(
        "-o",
        "--once",
        action="store_true",
        help=(
            "Make one connection/collection attempt and exit, instead of "
            "retrying indefinitely"
        ),
    )
    parser.add_argument(
        "-w",
        "--once-timeout",
        dest="once_timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help=(
            "Scan timeout and collection window to use in --once mode "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)

    if args.check_config:
        return _check_config(args.config)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR)
        _LOGGER.error(str(exc))
        return 1

    log_level = "DEBUG" if args.verbose else config.log_level
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        reading_received = asyncio.run(
            run_daemon(config, once=args.once, once_timeout=args.once_timeout)
        )
    except KeyboardInterrupt:
        return 0

    if args.once and not reading_received:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
