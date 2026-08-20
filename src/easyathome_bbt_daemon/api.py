"""Lightweight local HTTP API: liveness and capability discovery only.

This phase deliberately exposes just the two Hub-discoverable endpoints
addendum section 10 requires as a baseline (health, capabilities). Reading
retrieval, assignment actions, context-entry endpoints, dashboard/report
endpoints, and everything else in section 10's fuller list are out of scope
until their owning features (collection verification, context-entry UI,
dashboards, reporting, interpretation engines) exist -- see
``docs/HEALTH_HUB_BBT_DAEMON_ADDENDUM.md`` and this package's ``CLAUDE.md``.

All routes live under the ``/api/v1/`` prefix, matching the sibling
daemons' convention.
"""

from __future__ import annotations

import argparse

from aiohttp import web

from ._version import __version__
from .config import ApiConfig, ConfigError, is_insecurely_exposed, load_api_config, load_config


async def handle_health(request: web.Request) -> web.Response:
    """GET /api/v1/health -- unauthenticated liveness check."""
    return web.json_response({"status": "ok", "version": __version__})


async def handle_capabilities(request: web.Request) -> web.Response:
    """GET /api/v1/capabilities -- unauthenticated description of what this daemon supports.

    Deliberately honest about what is and is not implemented yet, so a
    Health Hub aggregator (or a person reading the raw JSON) doesn't assume
    more than this phase actually delivers. In particular:
    ``interpretation_modes`` lists only ``chart_only`` -- none of Sensiplan,
    SymptoPro, or TCOYF exist yet (addendum section 7) -- and
    ``hardware_verified`` is ``false`` because this daemon's BLE collection
    path has never run against a real EBT-300 (see the README warning
    banner and ``CLAUDE.md``).

    ``dashboards`` and ``report_generation`` are ``true`` as of this phase:
    ``chart.py`` renders the chart-only single-cycle view (addendum 7.1/8.1)
    and ``report.py`` generates immutable chart-only PDFs (addendum 9). Both
    are CLI-driven (``easyathome-bbt-report``), not HTTP-triggered -- the
    Hub-facing report-initiation/status/download API from addendum section
    10 is still a later phase, mirroring how the sibling
    ``health-thermometer-daemon`` keeps PDF generation CLI/cron-driven
    rather than API-triggered.
    """
    return web.json_response(
        {
            "daemon": "easyathome-bbt",
            "api_version": "v1",
            "measurement_types": ["temperature", "basal"],
            "interpretation_modes": ["chart_only"],
            "manual_entry": True,
            "assignment": True,
            "dashboards": True,
            "report_generation": True,
            "report_generation_trigger": "cli",
            "hardware_verified": False,
            "notes": (
                "Collection, persistence, chart-only dashboards, and chart-only PDF "
                "reporting (CLI-driven via easyathome-bbt-report) are implemented. The "
                "BBT context-entry web UI/form and the Sensiplan/SymptoPro/TCOYF "
                "interpretation engines are not implemented yet, nor is the Hub-facing "
                "report-initiation/status/download HTTP API -- see "
                "docs/HEALTH_HUB_BBT_DAEMON_ADDENDUM.md."
            ),
        }
    )


def build_app(api_config: ApiConfig) -> web.Application:
    """Build the aiohttp application with routes attached.

    Args:
        api_config: Reserved for when an authenticated endpoint is added in
            a later phase; the current two routes are intentionally
            unauthenticated (matching the sibling daemons' convention for
            health/capabilities).

    Returns:
        A configured, unstarted aiohttp Application.
    """
    app = web.Application()
    app["api_token"] = api_config.token
    app.router.add_get("/api/v1/health", handle_health)
    app.router.add_get("/api/v1/capabilities", handle_capabilities)
    return app


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="easyathome-bbt-api",
        description="Lightweight local HTTP API: health and capability discovery.",
    )
    parser.add_argument(
        "-c", "--config", required=True, help="Path to the daemon's INI config file"
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code. Only returns while disabled or on a config
        error -- otherwise blocks forever serving requests.
    """
    args = _parse_args(argv)

    try:
        load_config(args.config)  # validates [daemon]/[storage] too
        api_config = load_api_config(args.config)
    except ConfigError as exc:
        print(f"Error: {exc}")
        return 1

    if not api_config.enabled:
        print("API is disabled (api.enabled = no).")
        return 0

    if is_insecurely_exposed(api_config):
        print(
            f"WARNING: api.host is {api_config.host!r} (not loopback) but api.token "
            "is unset -- anyone who can reach this address can read health/capabilities "
            "data. Set api.token, or bind to 127.0.0.1 and put a reverse proxy with its "
            "own auth in front if you need remote access."
        )

    app = build_app(api_config)
    print(f"Listening on http://{api_config.host}:{api_config.port}")
    web.run_app(app, host=api_config.host, port=api_config.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
