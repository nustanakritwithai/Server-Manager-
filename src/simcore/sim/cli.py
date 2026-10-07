"""Command line for the simulator. ``python -m simcore.sim`` and ``simcore-sim``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from simcore.sim.runner import SimConfig, run
from simcore.sim.safety import SafetyError
from simcore.sim.thresholds import DEFAULT_COMMAND_RATE, DEFAULT_PLAYERS, DEFAULT_SEED


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="simcore-sim",
        description=(
            "Play the simulation with seeded bots over the HTTP API, then check "
            "traces, the ledger, armies, the audit chain, monitoring, and the snapshot checksum."
        ),
    )
    parser.add_argument("--players", type=int, default=DEFAULT_PLAYERS, help="Bot count (CI) or players to use (staging)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="RNG seed. The same seed replays the same commands")
    parser.add_argument("--duration", type=int, default=None, help="CI: total game seconds. Staging: total wall seconds")
    parser.add_argument("--ticks", type=int, default=None, help="Decision rounds. CI defaults to 4; each advances one game hour")
    parser.add_argument("--command-rate", type=int, default=DEFAULT_COMMAND_RATE, help="Commands each bot attempts per tick")
    parser.add_argument("--base-url", default=None, help="Staging API origin, for example http://127.0.0.1:8741")
    parser.add_argument("--mode", required=True, choices=("ci", "staging", "full"))
    parser.add_argument("--database-url", default=None, help="Defaults to SIMCORE_DATABASE_URL. Read-only checks, and the CI seed")
    parser.add_argument("--report-dir", default="sim-reports", help="Directory for report.json and report.md")
    parser.add_argument(
        "--i-understand-this-is-production",
        action="store_true",
        help="Allow a base URL or database that looks like production. Take a snapshot first.",
    )
    args = parser.parse_args(argv)
    token = _admin_token()
    config = SimConfig(
        mode=args.mode,
        players=args.players,
        seed=args.seed,
        ticks=args.ticks,
        duration=args.duration,
        command_rate=args.command_rate,
        base_url=args.base_url,
        database_url=args.database_url,
        report_dir=Path(args.report_dir),
        allow_production=args.i_understand_this_is_production,
        admin_token=token,
    )
    try:
        payload = run(config)
    except SafetyError as exc:
        print(f"simulator: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except Exception as exc:
        print(f"simulator: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    result = payload["result"]
    checksum = payload.get("world_checksum") or "n/a"
    print(
        f"{result} seed={payload['seed']} commands={payload['counts']['commands_accepted']} "
        f"events={payload['counts'].get('events')} battles={payload['counts'].get('battles')} "
        f"checksum={checksum}"
    )
    print(f"report: {config.report_dir / 'report.md'}")
    if result != "PASS":
        for item in payload.get("failed_invariants") or []:
            print(f"failed {item.get('invariant')}: {item.get('detail')}", file=sys.stderr)
        coverage = payload.get("coverage") or {}
        for gap in (coverage.get("gaps") or [])[:12]:
            print(f"coverage gap: {gap}", file=sys.stderr)
        raise SystemExit(1)


def _admin_token() -> str:
    import os

    return os.environ.get("SIMCORE_ADMIN_TOKEN", "").strip()


if __name__ == "__main__":
    main()
