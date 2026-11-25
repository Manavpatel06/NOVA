# 0027 — Talk on the API credit, build on the Max plan

Date: 2026-11-25. Manav: "when I'm just talking, then API key can be used. And when I ask Nova to build something
or anything, at that time it should use the max plan along with all the skills whatever are present on my plan."

## Decision
- **Talking** stays on the API credit (ADR 0026).
- **Building** — "Nova, build me …", a script, an app, a website, a write-up — goes to `build_project`, a hub skill
  (ACT; Claude's proposal of it is approved by voice). It starts **Claude Code** on the hub: the `claude` program
  signed in to the Max plan, with Manav's skills, plugins and MCP servers (no `--safe-mode`), headless.
- **Where projects go:** `~/Shared/Projects/<name>`, which the laptop sees as `H:\Projects\<name>` (ADR 0022).
  Asking again with the same project name keeps working in that folder.
- **One build at a time,** up to an hour and 200 turns. When it ends, the phone gets a fixed-text Bark ping.
  `build_status` reads back Claude's closing two-sentence summary. A restart marks an unfinished build failed.

## The sandbox (`nova/hub/builder.py`)
A build needs real tools (shell, installs, running code), so it runs with permission prompts off, inside
bubblewrap:
- It sees the OS read-only, a throwaway `/tmp` and home, the network, and its own project folder (read-write).
- It sees Claude Code's sign-in, so it can use the Max plan. Anything there that could later run code **outside**
  the sandbox is bound read-only: `settings.json`, `settings.local.json`, `CLAUDE.md`, `commands`, `agents`,
  `skills`, `plugins`, `hooks`, `output-styles`, `workflows`.
  - `~/.claude.json`, which lists MCP servers, is a throwaway copy.
  - So a hostile package installed during a build can't plant a hook or an MCP server that NOVA's unsandboxed
    brain would later run.
- It does not see `~/NOVA` (keys, phone settings, memory), the GitHub sign-in, SSH keys, D-Bus or other projects.

## Verified
- A fake `claude` inside the real sandbox in the build environment tried to break out:
  - It could write its project, Claude's session state, and its copy of `.claude.json`.
  - It was blocked from `settings.json` and `skills/`.
  - It could not see the NOVA repo, `~/.ssh`, the gh config, `/run/dbus` or a sibling project.
  - The real `settings.json` and `.claude.json` were unchanged.
- A real Claude Code run couldn't be tested here: the build environment's sign-in depends on its own proxy. On
  the hub, the forge's builder uses the same sandbox pattern.
