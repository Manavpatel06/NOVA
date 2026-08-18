---
name: verify-claims
description: Checklist for independently verifying an agent's work report — test counts, fix claims, and environment excuses. Use before marking any task complete.
---

# Verify a work report

Given a teammate's report claiming work is done, verify it in this order. Do not skip steps because the report "looks thorough" — polish and accuracy are uncorrelated.

1. **Re-run the suite yourself.** Compare exact collected / passed / failed counts against the report. Any mismatch, however small, is a finding — report the real numbers.
2. **Revert-check each claimed fix.** `git stash` or revert the named change, re-run the relevant tests, confirm the failure reproduces, restore. If tests stay green on revert, a different change did the work — the report credits the wrong line.
3. **Check claims have timestamps' honesty.** "Exactly one failure" is a claim about one run at one moment. Re-run at least twice; anything flaky gets investigated, not averaged away.
4. **Audit environment excuses.** If the report blames the environment (CPU, sandbox, network), read the failing test's own source and docstring first. Confirm the stated mechanism is real (e.g., is the dependency actually missing?) before accepting it.
5. **Spot-check the diff against the description.** Files changed but not mentioned, or mentioned but not changed, are findings.
6. **Report discrepancies** to the lead as: claim → what you ran → what you actually observed. A wrong report is a bug even when the code is fine.
