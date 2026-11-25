# 0022 — The hub as a shared drive and backup target

Date: 2026-11-25. Manav asked whether the hub's spare storage, RAM and compute could take load off his
laptop. The answer: storage and backups help a lot (C: is full), and they cost the hub almost nothing.
Moving compute off the laptop mostly doesn't help, because the hub is slower than the laptop and adds network
delay. The hub already takes the work that suits it: the local model, the Claude brain, reminders and the
forge.

## What it is
- `nova-hub share` installs Samba and shares `~/Shared` on the hub as `\\nova-hub\nova`.
- `scripts/hub_drive.ps1 -User <name>` maps it as `H:` on the laptop. Windows prompts for the password
  (`cmdkey /pass`), and Windows remembers it. NOVA never sees it.
- `hub_drive.ps1 -Backup <folders>` can add a daily 02:30 scheduled task. The task robocopy-mirrors those
  folders to `\\nova-hub\nova\backups\<name>` and skips venvs, caches, `node_modules` and models. Manav
  answers y/n in the script before the task is created. `-Remove` undoes everything on the laptop.

## How it is kept private
Samba can't bind to `tailscale0`: it skips interfaces that aren't broadcast ("not adding non-broadcast
interface"), and Tailscale's is point-to-point. That was confirmed on the real hub and reproduced with a
point-to-point /32 tun device. So smbd listens on all addresses, and four independent layers keep the share
Tailscale-only:
1. **UFW** (ADR 0019) accepts incoming traffic only on `tailscale0`.
2. **systemd `RestrictNetworkInterfaces=lo tailscale0`** on smbd: the kernel drops smbd's packets on any
   other interface. The filter is fixed when smbd starts, so smbd has `PartOf=tailscaled.service` and
   restarts with Tailscale. It also starts after Tailscale and waits for `tailscale0`.
3. **systemd `IPAddressDeny=any` / `IPAddressAllow=localhost 100.64.0.0/10 fd7a:115c:a1e0::/48`** on smbd.
4. **Samba `hosts allow`** with the same ranges. Tested: a connection from a 192.168.x address is dropped
   while the Tailscale address works.

`nova-hub share` checks that UFW is active, that both systemd filters show on smbd and that smbd is running.
If any is missing it stops smbd and says so.

Other protections:
- Connections require SMB3 with encryption, so traffic is encrypted twice (WireGuard, then SMB). Tested:
  SMB2 is refused.
- Access:
  - Manav's own account is the only valid user.
  - There are no guests, no anonymous access, no NetBIOS and no printers. Tested: a wrong password and an
    anonymous login are refused.
  - Files are created 0640 and folders 0750.
- The original `smb.conf` is kept as `smb.conf.before-nova`.

## Limits
- Backups are a **mirror, not a history**: deleting a file on the laptop deletes it from the backup on the
  next run. The hub's own nightly snapshot covers NOVA's memory only. Versioned backups of `~/Shared` (for
  example restic) are a later step if Manav wants them.
- The hub has 512 GB in one disk, so one copy on the hub is not a full backup. It protects against the laptop
  failing, not against the hub's disk failing.
- The share is reachable only while Tailscale is on, on both machines.
