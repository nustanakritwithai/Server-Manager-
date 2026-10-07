"""``python -m simcore.load`` and ``simcore-load``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from simcore.load.probe import run_probe
from simcore.load.runner import LoadConfig, run
from simcore.load.safety import LoadSafetyError

_PROFILES = {
    "ci": {
        "players": 4,
        "duration": 5.0,
        "rate": 8.0,
        "concurrency": 4,
        "seed": 8741,
        "failures": True,
        "mode": "local",
    },
    "long": {
        "players": 12,
        "duration": 30.0,
        "rate": 20.0,
        "concurrency": 8,
        "seed": 8741,
        "failures": True,
        "mode": "local",
    },
}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "probe":
        _probe(argv[1:])
        return
    _run(argv)


def _run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="simcore-load",
        description=(
            "Register players through the real auth flow, send mixed command traffic, "
            "and (in local mode) inject failures. Measurements are reported as measured. "
            "Missing instruments stay UNKNOWN or NOT INSTRUMENTED."
        ),
    )
    parser.add_argument("--profile", choices=tuple(_PROFILES), default=None)
    parser.add_argument("--mode", choices=("local", "external"), default=None)
    parser.add_argument("--players", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--rate", type=float, default=None, help="Target requests per second")
    parser.add_argument("--duration", type=float, default=None, help="Load phase length in seconds")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--report-dir", default="load-reports")
    parser.add_argument(
        "--failures",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Local mode only. External mode refuses failure injection.",
    )
    parser.add_argument(
        "--i-understand-this-is-production",
        action="store_true",
        help=(
            "Required, together with the low rate cap, before a non-localhost URL is contacted. "
            "Does not allow failure injection and does not raise the cap."
        ),
    )
    args = parser.parse_args(argv)
    chosen = dict(_PROFILES["ci"])
    if args.profile:
        chosen.update(_PROFILES[args.profile])
    profile_name = args.profile or "ci"
    if args.mode is not None:
        chosen["mode"] = args.mode
    if args.players is not None:
        chosen["players"] = args.players
    if args.seed is not None:
        chosen["seed"] = args.seed
    if args.rate is not None:
        chosen["rate"] = args.rate
    if args.duration is not None:
        chosen["duration"] = args.duration
    if args.concurrency is not None:
        chosen["concurrency"] = args.concurrency
    if args.failures is not None:
        chosen["failures"] = args.failures
    database_url = args.database_url or os.environ.get("SIMCORE_DATABASE_URL")
    config = LoadConfig(
        mode=str(chosen["mode"]),
        players=int(chosen["players"]),
        seed=int(chosen["seed"]),
        rate=float(chosen["rate"]),
        duration=float(chosen["duration"]),
        concurrency=int(chosen["concurrency"]),
        failures=bool(chosen["failures"]),
        base_url=args.base_url,
        database_url=database_url,
        report_dir=Path(args.report_dir),
        allow_production=args.i_understand_this_is_production,
        admin_token=_admin_token(),
        profile=profile_name,
    )
    try:
        payload = run(config)
    except LoadSafetyError as exc:
        print(f"load: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    result = payload["result"]
    load = payload.get("load") or {}
    latency = (load.get("latency_all") or {})
    throughput = (load.get("throughput") or {})
    print(
        f"{result} profile={payload.get('profile')} seed={payload.get('seed')} "
        f"load_requests={load.get('requests')} "
        f"p50_ms={latency.get('p50_ms')} p95_ms={latency.get('p95_ms')} p99_ms={latency.get('p99_ms')} "
        f"rps={throughput.get('achieved_rps')}"
    )
    print(f"report: {config.report_dir / 'report.md'}")
    if result != "PASS":
        for item in payload.get("checks") or []:
            if item.get("status") != "PASS":
                print(f"{item.get('status')} {item.get('name')}: {item.get('detail')}", file=sys.stderr)
        raise SystemExit(1)


def _probe(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="simcore-load probe",
        description="Send GET /health and GET /health/ready. Two requests, no commands, no failure injection.",
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--report-dir", default="load-reports/probe")
    parser.add_argument("--i-understand-this-is-production", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = run_probe(
            base_url=args.base_url,
            allow_production=args.i_understand_this_is_production,
            report_dir=Path(args.report_dir),
        )
    except LoadSafetyError as exc:
        print(f"load probe: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(f"{payload['result']} probe requests={payload['requests']}")
    print(f"report: {Path(args.report_dir) / 'report.md'}")
    if payload["result"] != "PASS":
        raise SystemExit(1)


def _admin_token() -> str:
    token = os.environ.get("SIMCORE_ADMIN_TOKEN", "").strip()
    if token:
        return token
    if os.environ.get("SIMCORE_ENV", "development") == "production":
        raise LoadSafetyError("SIMCORE_ADMIN_TOKEN is required when SIMCORE_ENV=production")
    return "dev-admin"


if __name__ == "__main__":
    main()
