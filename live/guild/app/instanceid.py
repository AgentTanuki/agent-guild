"""Process identity — the decidability layer for state divergence.

WHY THIS EXISTS (divergence incident 2026-07-30/31)
---------------------------------------------------
Production served two mutually inconsistent views of the same counters within
minutes: ``/instrumentation`` returned a frozen older snapshot on several
consecutive reads before flipping to the current one, and a ``POST
/ledger/checkpoint/publish`` returned checkpoint index 14 / ledger_length 834
while the published feed was already at 16 / 836.

The investigation could NOT decide between the candidate causes — a second
serving origin, an intermediary serving a stale body, or an in-process stale
durable read — because NO RESPONSE CARRIED ANYTHING THAT IDENTIFIED WHICH
PROCESS OR WHICH STATE PRODUCED IT. Every candidate explanation was equally
consistent with the evidence. That is a diagnosability defect, and it is fixed
here rather than guessed around.

WHAT IS STAMPED (all non-secret, all safe to publish)
  * ``instance``  — random per-PROCESS id, minted at import. Two different
    values observed for one release SHA PROVE more than one serving process.
    One value across a divergent pair DISPROVES the split-origin theory and
    points at an intermediary or an in-process stale read.
  * ``boot_at``   — process start (UTC). Distinguishes "restarted" from
    "second instance" when ids differ.
  * ``pid``       — process id inside the container. Distinguishes forked
    workers that share a boot timestamp.
  * ``store_rev`` — monotonic in-memory mutation counter (``Store.revision``).
    A response whose ``store_rev`` is LOWER than one already observed from the
    same ``instance`` is a stale in-process view; across instances it is a
    split-brain read. Either way it becomes DETECTABLE FROM OUTSIDE.

It deliberately leaks no paths, tokens, environment or hostnames — the id is
random, not derived from anything sensitive, so a third party cannot correlate
it back to infrastructure.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

#: Random per-process identity. Minted once at import; never persisted, never
#: derived from a hostname/path/secret.
INSTANCE_ID: str = secrets.token_hex(6)

#: Process start time (UTC, ISO-8601).
BOOT_AT: str = datetime.now(timezone.utc).isoformat()

#: OS process id — separates forked workers that share a boot timestamp.
PID: int = os.getpid()


def identity() -> dict[str, object]:
    """The non-secret process identity block embedded in diagnostics."""
    return {"instance": INSTANCE_ID, "boot_at": BOOT_AT, "pid": PID}
