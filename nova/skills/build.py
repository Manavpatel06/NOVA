"""Building things (ADR 0027): "Nova, build me a ..." -- Claude Code on
Manav's Max plan, with all his skills, works on it on the hub while NOVA keeps
talking on the API credit. The project lands in H:\\Projects on his laptop."""
from __future__ import annotations

from nova.brain.hub_tier import HubUnavailable, hub_post
from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

logger = get_logger("skills.build")


@skill(
    name="build_project",
    tier=Tier.ACT,
    examples=["build me a website for my portfolio", "make a script that renames my photos by date",
              "create an app that tracks my expenses"],
    description=("Start real work that takes minutes: build software, an app, a website, a script, a tool, or "
                 "a document/report written to files. Runs Claude Code on Manav's Max plan with all his skills, "
                 "in a project folder he opens on his laptop as H:\\Projects\\<name>. Use this whenever he asks "
                 "NOVA to build, make, code or create something substantial (not for quick answers). Params: "
                 "request (everything he asked for, in his words), name (optional short project name; reuse an "
                 "existing name to keep working on that project)."),
    trusts_input=False,
    patterns=[r"(?:build|code|program) (?:me )?(?P<request>.+)"],
    confirm_phrase="start building {request} with Claude Code",
)
async def build_project(ctx: SkillCtx, request: str, name: str = "") -> Reply:
    try:
        out = await hub_post(ctx.config, ctx.egress, "/v1/build/start",
                             {"request": request[:500], "name": name[:40]}, timeout_s=15.0)
    except HubUnavailable as exc:
        logger.warning("build_project: %s", exc)
        return Reply(text=f"I couldn't start that build: {exc}.", success=False)
    what = "Picking up" if out.get("continuing") else "Building"
    return Reply(text=f"{what} {out.get('project', 'it')} now with Claude Code on your Max plan. "
                      "I'll ping your phone when it's done; it'll be in H:\\Projects.")


@skill(
    name="build_status",
    tier=Tier.READ,
    examples=["how's the build going", "is my project done", "what did you build"],
    description="How NOVA's latest builds went (Claude Code projects): building, done or failed, and the summary.",
    trusts_input=False,
    patterns=[r"(?:how(?:'s| is) (?:the|my) (?:build|project)(?: going)?|is (?:the|my) (?:build|project) (?:done|ready))"],
)
async def build_status(ctx: SkillCtx) -> Reply:
    try:
        out = await hub_post(ctx.config, ctx.egress, "/v1/build/list", {})
    except HubUnavailable:
        return Reply(text="I can't reach the hub to check.", success=False)
    builds = out.get("builds") or []
    if not builds:
        return Reply(text="I haven't built anything yet.")
    b = builds[0]
    if b.get("status") == "building":
        return Reply(text=f"Still building {b.get('project')}.")
    if b.get("status") == "done":
        return Reply(text=f"{b.get('project')} is done. {b.get('summary', '')}".strip())
    return Reply(text=f"{b.get('project')} didn't finish. {b.get('summary', '')}".strip(), success=False)
