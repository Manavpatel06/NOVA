"""NOVA upgrading itself, by voice (2026-11-24, docs/decisions/0021).

    "Nova, add a skill that tells me a joke"        -> upgrade_nova: the hub builds it
    "Nova, how's the upgrade going?"                 -> upgrade_status
    "Nova, apply the upgrade" ... "Nova, confirm"    -> apply_upgrade (CONFIRM tier)
    "Nova, throw away the upgrade"                   -> discard_upgrade

Both steps are CONFIRM. Building: NOVA reads the request back before the
hub spends anything on it. Installing: NOVA reads back exactly what will be
installed (the request, the new files, the commit) and only Manav's "Nova,
confirm" puts it in. The hub then installs and pushes that exact commit, and
this laptop installs that same commit (nova.system.winops.git_update) --
never whatever else might be on GitHub -- and asks to be reopened.
"""
from __future__ import annotations

import asyncio

from nova.brain.hub_tier import HubUnavailable, hub_post
from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

logger = get_logger("skills.upgrades")

NO_HUB = "I build upgrades on the hub, and I can't reach it right now."
_SAY = {
    "queued": "is about to start", "building": "is being built", "testing": "is being tested",
    "reviewing": "is being reviewed", "ready": "is ready", "failed": "didn't work out",
    "blocked": "was stopped", "applied": "is installed", "discarded": "was thrown away",
}


@skill(
    name="upgrade_nova",
    tier=Tier.CONFIRM,
    examples=["add a skill that tells me a joke", "upgrade yourself so you can flip a coin",
              "teach yourself to convert units", "add a feature to read my battery percentage aloud"],
    description=("Have NOVA build a new ability for itself on the hub: Claude Code writes it with tests, the "
                 "hub tests and reviews it, and Manav installs it later by saying 'apply the upgrade'. Nothing "
                 "changes until then. One param: request -- what to add, in Manav's own words."),
    trusts_input=False,
    patterns=[
        r"(?:upgrade|improve) yourself (?:so (?:that )?you can |to )(?P<request>.{5,500})",
        r"teach yourself (?:to |how to )(?P<request>.{5,500})",
        r"add (?:a |an )?(?:new )?(?:skill|feature|ability|command) (?:to yourself )?(?:that |which |to |for |so )"
        r"(?P<request>.{5,500})",
    ],
    confirm_phrase="start building an upgrade so I can {request}",
)
async def upgrade_nova(ctx: SkillCtx, request: str) -> Reply:
    request = " ".join(request.split())[:500]
    if len(request) < 5:
        return Reply(text="What should I add?", success=False)
    try:
        out = await hub_post(ctx.config, ctx.egress, "/v1/forge/request", {"request": request}, timeout_s=15.0)
    except HubUnavailable:
        return Reply(text=NO_HUB, success=False)
    if not out.get("ok"):
        return Reply(text=f"I can't start that: {out.get('error', 'the hub said no')}.", success=False)
    logger.info("upgrade requested (%s)", out.get("id"))
    return Reply(text="On it. I'm building that on the hub; it usually takes 10 to 30 minutes. "
                      "I'll ping your phone when it's ready for you to approve.")


@skill(
    name="upgrade_status",
    tier=Tier.READ,
    examples=["how's the upgrade going", "is the upgrade ready", "what upgrades are there"],
    description="Say how NOVA's latest self-upgrade is doing (building, ready, failed...). No params.",
    trusts_input=False,
    patterns=[
        r"(?:how(?:'s| is) the|is the|what(?:'s| is) the) upgrade(?: going| ready| status| doing)?",
        r"(?:any|what) upgrades(?: are there| ready)?",
    ],
)
async def upgrade_status(ctx: SkillCtx) -> Reply:
    try:
        out = await hub_post(ctx.config, ctx.egress, "/v1/forge/list", {})
    except HubUnavailable:
        return Reply(text=NO_HUB, success=False)
    jobs = out.get("jobs") if isinstance(out, dict) else None
    if not jobs:
        return Reply(text="There aren't any upgrades yet.")
    job = jobs[0]
    status = str(job.get("status", ""))
    text = f"The upgrade to {str(job.get('request', ''))[:80]} {_SAY.get(status, status)}."
    if status == "ready":
        text += " " + str(job.get("summary", ""))[:300] + " Say 'apply the upgrade' to install it."
    elif status in ("failed", "blocked"):
        text += " " + str(job.get("detail", ""))[:160]
    return Reply(text=text.strip())


async def _ready(ctx: SkillCtx) -> dict | None:
    out = await hub_post(ctx.config, ctx.egress, "/v1/forge/ready", {})
    ready = out.get("ready") if isinstance(out, dict) else None
    if isinstance(ready, dict) and all(isinstance(ready.get(k), str) for k in ("id", "commit", "request")):
        return ready
    return None


async def _what_will_be_installed(ctx: SkillCtx, params: dict) -> dict:
    """Shown in the confirm prompt, and checked again after Manav answers:
    if the ready upgrade changed in between, nothing is installed."""
    ready = await _ready(ctx)
    if ready is None:
        return {"upgrade": "nothing -- no upgrade is ready"}
    files = ", ".join(str(f).rsplit("/", 1)[-1] for f in ready.get("files", [])[:6])
    return {"upgrade": f"{ready['request'][:120]} (new files: {files}; version {ready['commit'][:7]})"}


@skill(
    name="apply_upgrade",
    tier=Tier.CONFIRM,
    examples=["apply the upgrade", "install the upgrade", "yes install it"],
    description=("Install NOVA's ready self-upgrade: the hub adds it to NOVA and this laptop installs that exact "
                 "version. Needs Manav's explicit confirmation. No params."),
    trusts_input=False,
    patterns=[r"(?:apply|install|accept) (?:the |that |this )?(?:upgrade|new skill|new feature)"],
    confirm_phrase="install the upgrade: {upgrade}",
    resolve_for_confirm=_what_will_be_installed,
)
async def apply_upgrade(ctx: SkillCtx, upgrade: str = "the ready one") -> Reply:
    try:
        ready = await _ready(ctx)
        if ready is None:
            return Reply(text="There's no upgrade ready to install.", success=False)
        out = await hub_post(ctx.config, ctx.egress, "/v1/forge/apply",
                             {"id": ready["id"], "commit": ready["commit"]}, timeout_s=150.0)
    except HubUnavailable:
        return Reply(text=NO_HUB, success=False)
    if not out.get("ok"):
        return Reply(text=f"I couldn't install it: {out.get('error', 'the hub said no')}.", success=False)
    if not out.get("pushed"):
        return Reply(text="It's installed on the hub, but the hub couldn't send it to GitHub, so this laptop "
                          "can't get it yet. Run 'nova-hub github' on the hub once.", success=False)
    from nova import ROOT
    from nova.system import winops

    try:
        await asyncio.to_thread(winops.git_update, str(ROOT), ready["commit"])
    except Exception as exc:  # noqa: BLE001
        logger.error("apply_upgrade: install here failed (%s)", exc)
        return Reply(text="The upgrade is on GitHub, but I couldn't install it on this laptop safely. "
                          "Ask me about it again later.", success=False)
    logger.info("apply_upgrade: installed %s", ready["commit"][:8])
    return Reply(text="Installed. Close and reopen me to start using it.")


@skill(
    name="discard_upgrade",
    tier=Tier.ACT,
    examples=["throw away the upgrade", "discard the upgrade", "cancel the upgrade"],
    description="Throw away NOVA's latest self-upgrade that hasn't been installed. No params.",
    trusts_input=False,
    patterns=[r"(?:discard|throw away|cancel|delete|scrap) (?:the |that |this )?upgrade"],
)
async def discard_upgrade(ctx: SkillCtx) -> Reply:
    try:
        out = await hub_post(ctx.config, ctx.egress, "/v1/forge/discard", {"id": "latest"})
    except HubUnavailable:
        return Reply(text=NO_HUB, success=False)
    return Reply(text="Thrown away." if out.get("ok") else "There's nothing to throw away.")
