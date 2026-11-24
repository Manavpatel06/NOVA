# 0021 — NOVA's brain runs on the hub, and NOVA can upgrade itself

Date: 2026-11-24. Manav: "Complete NOVA then I can put its brain in my hub, and also I want NOVA to be self
improvable: if I ask it to add something to it, NOVA should be able to upgrade itself."

## 1. The brain on the hub
- The hub runs the Max-plan `claude` conversation (`nova/hub/brain_host.py`, one session, owner-only
  `/v1/brain/start|send|close`). The laptop's MaxBrain talks to it via `nova/brain/remote_session.py`.
- **Tools still run on the laptop** through `SkillSpec.invoke()`: same ladder, audit and spoken approval.
  The hub only produces Claude's replies.
- `brain.where`:
  - `auto` (default): the hub when `hub.host` is set. If the hub can't be reached before a conversation
    starts, the laptop's own `claude` is used. NOVA never switches mid-conversation.
  - `hub`: hub only.
  - `laptop`: never the hub.
- Taint fails closed: if the hub's reply doesn't say which tools were used and web search is on, the
  conversation counts as tainted, so actions need "Nova, confirm".
- The hub becomes part of NOVA's trusted base: its replies choose actions on the laptop. It is reachable only
  by Manav's Tailscale account and only by its own name (ADR 0019).

## 2. Self-upgrade: the Forge (`nova/hub/forge.py`)
**Two confirmations.** "Nova, add a skill that ..." is `upgrade_nova`, CONFIRM tier: NOVA reads the request
back before building. "Nova, apply the upgrade" is `apply_upgrade`, CONFIRM tier: NOVA reads back the
request, the new files and the commit, and checks they haven't changed before acting.

**What an upgrade may contain:**
- Only **new** files, and only `nova/skills/<name>.py` and `tests/test_<name>.py`. Up to 8 files, 64 KB
  each, regular files only.
- Nothing existing can be changed or deleted. That covers the ladder, security code, configs, test
  settings, CLAUDE.md and the forge itself.

**Static check on every new file** (`static_problems`):
- Imports: pure stdlib modules, plus `from X import name` of a short list of NOVA functions. Those are the
  skill decorator, tiers, logging, safe winops helpers, time parsing and memory.
- Nothing that leads to a process, a socket, a file write, the hub's owner-only calls or the approval
  internals.
- Not allowed: eval/exec/open/getattr/type, any `_private` attribute, assigning to other objects'
  attributes, or long key-like strings.
- Every known way around the check has a test.

**Builder.** Claude Code headless on the Max plan, with Read/Edit/Write/Glob/Grep only: no shell, no web,
`--safe-mode`. It works on a plain export of master with no `.git`, inside bubblewrap. It can write only that
folder, can reach its own sign-in and the network, and cannot see Manav's home, `~/NOVA` or the GitHub token.

**Tests**, run with bubblewrap: no network, no `/home`, `/run` or `/var`, the OS read-only, only the tree
writable.
- Master's whole suite runs with the new test files ignored. Its failures may not grow and its passes may
  not shrink.
- The new tests then run in their own process.

**Reviewer.** A read-only Claude reviews the new files, marked as untrusted, against CLAUDE.md. It gets one
fix round; changes too large to review fail closed.

**Commit.** Built with git plumbing in the hub's repo from the checked bytes: hooks off, no filters. It is
authored like master's last commit, as Manav asked: no Claude trailer.

**Install.**
- The hub fast-forwards master to exactly the approved commit, and only if master hasn't moved, then
  pushes.
- The laptop runs `winops.git_update(repo, commit)`: it fetches, requires GitHub's master to equal that
  commit, then fast-forwards with hooks off. NOVA then asks to be reopened.

**One at a time.** A new build waits until the ready one is applied or thrown away. Builds cut short by a
restart are marked failed. Bark pings use fixed text only.

**Setup.**
- `setup_hub.sh` step 11 installs the workshop: a test venv in `~/NOVA-forge`, bubblewrap and gh.
- `nova-hub login` adds GitHub sign-in (`nova-hub github`).
- `nova-hub upgrades` lists builds.

## Known limits
- Upgrades can only **add** skills. Changing existing behaviour ("make the volume step bigger") is refused.
  Manav asks Claude in Cowork for those.
- The builder's sandbox (Claude Code inside bubblewrap) has not been run on the real hub yet. If Claude Code
  won't start inside it, upgrades fail with "the builder stopped" until it's adjusted; nothing runs
  unsandboxed.
- A forged skill could still do the wrong thing within its allowed APIs (for example the wrong tier). The
  reviewer and Manav's confirmation, which names the files, are the checks for that.
