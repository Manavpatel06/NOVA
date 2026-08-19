"""Human-runnable CONFIRM dialog probe -- the thing an agent cannot verify.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_confirm_dialog.py

This is the exact failure the retired build had for most of its life: a
CONFIRM dialog that existed in code but nobody ever verified against a human
actually clicking it, because the dev box had no display. This script drives
`DialogConfirmChannel` through the real `Ladder` (a real temp AuditLog, not a
mock) three times, once per outcome, and tells you what to click each time.

What to check, in order:

  Round 1 -- APPROVE:
    A real Windows message box pops up titled "NOVA — approve
    probe_confirm_open_app?" showing the skill name, tier, and the (redacted)
    params it would run with. Click **Yes**. This script should print
    `Decision(allowed=True, reason=None)`.

  Round 2 -- DECLINE:
    Same box. Click **No**. Should print `Decision(allowed=False,
    reason='confirm_declined')`.

  Round 3 -- TIMEOUT:
    Same box, but this time don't click anything for 8 seconds. The script
    gives up and should print `Decision(allowed=False,
    reason='confirm_timed_out')` -- and, importantly, control returns to
    this script immediately at the 8s mark; it does not wait for you to
    close the box. The box will still be sitting on your screen after this
    script exits -- that's the documented limitation (see
    DialogConfirmChannel's docstring: a plain Yes/No box can't be dismissed
    from another thread). Click it or close it whenever; check that doing so
    does NOT print anything else here (the decision was already made and
    audited before you touched it) -- close it manually once you've
    confirmed that.

Also prints the raw audit records written for all three rounds, redacted,
so you can see CONFIRM produces exactly one audit entry per round, with the
right reason.

Not part of the test suite: this one needs a mouse.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from nova.security.audit import AuditLog
from nova.security.confirm import DialogConfirmChannel
from nova.security.ladder import Ladder
from nova.security.tiers import Tier


@dataclass(frozen=True)
class _FakeConfirmSkill:
    name: str = "probe_confirm_open_app"
    tier: Tier = Tier.CONFIRM


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        audit_path = Path(tmp) / "probe_audit.jsonl"
        audit = AuditLog(audit_path)
        channel = DialogConfirmChannel(timeout_s=8.0)
        ladder = Ladder(audit, confirm_channel=channel)
        skill = _FakeConfirmSkill()
        params = {"app": "notepad", "reason": "user asked to open notepad"}

        print("Round 1/3 -- a dialog will appear. Click YES.")
        decision = await ladder.authorize(skill, params)
        print(f"  -> {decision}\n")

        print("Round 2/3 -- a dialog will appear. Click NO.")
        decision = await ladder.authorize(skill, params)
        print(f"  -> {decision}\n")

        print("Round 3/3 -- a dialog will appear. Do NOT click anything for 8 seconds.")
        decision = await ladder.authorize(skill, params)
        print(f"  -> {decision}")
        print("  (the box may still be on-screen -- that's expected, see this file's docstring)\n")

        print("Audit records written (redacted, one per round):")
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            print(f"  {line}")

    print("\nDid round 1 say allowed=True, round 2 confirm_declined, round 3 confirm_timed_out?")
    print("If yes, record it in docs/EVIDENCE.md.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
