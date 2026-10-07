"""Seeded bot players that exercise the simulation through the HTTP API.

Gameplay commands go to the public player routes. CI mode advances the
existing admin clock and drains the existing worker. Verification reads
server traces, the audit chain, monitoring, the snapshot checksum, and
read-only rows. It does not write the world in order to make a check pass.
"""

__all__ = ["run"]


def run(*args, **kwargs):
    from simcore.sim.runner import run as _run

    return _run(*args, **kwargs)
