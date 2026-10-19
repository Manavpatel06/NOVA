# 0015 — Voice-first control: spoken approval, command patterns, ~40 skills

Date: 2026-10-15. Decided by Manav ("manage the actions from my voice instructions rather than me clicking anything").

## Decisions
- **Spoken approval** (`nova/security/voice_confirm.py`). When an action needs approval NOVA asks "Should I …?" and listens once. A plain "yes / yeah / sure / do it / go ahead" approves an ordinary action; a skill whose own tier is CONFIRM (shutdown, restart, sign out, sleep) needs "Nova, confirm". Anything else, or silence, is a no. The Yes/No dialog remains only as the fallback when the mic is off. Answers are never shown in the window. Known limit: no speaker verification — anyone in the room can answer.
- **Command patterns** (`@skill(patterns=..., catch_all_patterns=...)`, `Router._match_patterns`). Regexes over the normalized sentence, named groups → params, so multi-part commands work ("set volume to 40"). Two skills matching = clarification. Catch-all patterns (open_app's "open X") count only when no specific pattern matched. Fuzzy example matching and the Claude fallthrough are unchanged behind them.
- **One OS doorway** (`nova/system/winops.py`), the fifth process-spawn allowlist entry: constant PowerShell scripts run with -EncodedCommand, user-derived values only as JSON data; System32 programs with fixed args; https-only URLs; Start-menu launch by Windows' own AppID.
- **open_app beyond the allowlist**: unknown names resolve against Windows' Get-StartApps list and launch via shell:AppsFolder (ACT). This deliberately widens what voice can open (Spotify, Discord, VS Code, Store apps); speech only selects among Windows' own entries. Reviewed 2026-10-15: no privilege boundary crossed.
- **New skills**: window/app control (close/minimize/maximize/switch, show desktop, lock; sleep/shutdown/restart/sign-out at CONFIRM with a 15 s NOVA-side grace timer and "cancel shutdown"), system settings (volume, brightness, Wi-Fi/Bluetooth, battery, screenshot, settings pages), web & media (Google/YouTube search, websites, play/pause/next/previous/stop, weather via Open-Meteo through Egress, date).
- **Whisper hints**: hotwords now carry common command words after the wake name.

## Not yet
Gmail, Google Calendar, files, reminders/timers (next round); a faster Claude path via the API for sentences no pattern matches.
