# 0031 — Running both machines by voice: hub status, restart, open any file

Date: 2026-11-25. Manav: "if I want to open something in my new laptop, or if I want to do something in my hub, I want
to be able to manage that as well ... with voice."

## Hub (hub-only skills, `nova/hub/hub_skills.py`, never offered on the laptop)
- **hub_status** (READ): "Nova, how's the hub?" -- up time, battery and whether it's charging (says so when the HP is
  on battery), temperature, memory, free space, the laptop link, connected apps (and which aren't working), and this
  month's API credit. Read from /proc and /sys only; nothing is run.
- **restart_nova** (ACT): NOVA restarts its own service; systemd brings it back in ~20 s.

## Laptop (`open_file`, runs there through NOVA Link)
- "Nova, open my resume / the CSE 576 folder / the budget spreadsheet": searches Desktop, Documents, Downloads,
  Pictures, Videos, Music, OneDrive and the drive NOVA lives on (D:), skipping hidden and system folders and NOVA's own
  folder, and opens the best match with its usual app (folders in File Explorer).
- The spoken name is only a search string. What opens is a path found inside those folders, re-checked with realpath
  when it opens (`winops.open_document`, rule 3); programs, scripts, shortcuts, macro documents and disk images are
  never opened (`winops.RUNNABLE`); no network paths.
- Two equally good matches: NOVA names them and asks which. The approval question, when one is needed (after outside
  content), shows the full path.

## Not yet
- Updating NOVA and rebooting the hub by voice: they need the hub to run commands, which goes through a reviewed
  runner; still an idea. Today: `nova-hub update` at the hub.
- "Do anything on either machine" with Claude Code (plan, then "Nova, confirm"): Exploring.
