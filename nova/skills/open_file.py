"""open_file -- open a document or folder by name, by voice (ACT).

Manav, 2026-11-25: "if I want to open something in my new laptop ... I want
to be able to manage that as well ... with voice".

"Open my resume", "open the CSE 576 folder", "open the budget spreadsheet":
NOVA looks through his own folders (Desktop, Documents, Downloads, Pictures,
Videos, Music, OneDrive) and D:, picks the best match by name, and opens it
with its usual app (a folder opens in File Explorer).

Safety:
  * the spoken name is only a search string; what's opened is a path found
    inside those folders, re-checked with realpath when it opens (a junction
    pointing elsewhere is refused) -- CLAUDE.md rule 3;
  * programs, scripts, shortcuts and macro documents are never opened
    (winops.RUNNABLE): this opens documents, it doesn't run things;
  * no network paths, nothing outside those folders, nothing hidden;
  * when two files match about equally, NOVA names them and asks which,
    instead of guessing;
  * the approval question (when one is needed) shows the full path found.
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
from pathlib import Path

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

_log = get_logger("skills.open_file")

USER_FOLDERS = ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")
SKIP_DIRS = frozenset({"appdata", "node_modules", ".git", "__pycache__", ".venv", "venv", "site-packages",
                       "$recycle.bin", "system volume information", "windows", "program files",
                       "program files (x86)", "programdata", ".cache"})
MAX_ENTRIES = 60_000
MAX_DEPTH = 7
MIN_SCORE = 0.62
TIE = 0.04
FILLER = re.compile(r"\b(?:the|my|a|an|file|document|doc|folder|directory|please|called|named|for me|"
                    r"spreadsheet|sheet|presentation|slides|deck|pdf|photo|picture|video|notes? file)\b")


def search_roots() -> list[Path]:
    home = Path.home()
    roots = [home / f for f in USER_FOLDERS]
    roots += sorted(p for p in home.glob("OneDrive*") if p.is_dir())
    if os.name == "nt":   # the drive NOVA lives on (D:, C: is full): Manav keeps his work there too
        from nova import ROOT

        drive = Path(Path(ROOT).anchor)
        if drive.is_dir() and drive != Path(Path.home().anchor):
            roots.append(drive)
    seen, out = set(), []
    for r in roots:
        if r.is_dir() and str(r).lower() not in seen:
            seen.add(str(r).lower())
            out.append(r)
    return out


def _norm(text: str) -> str:
    text = FILLER.sub(" ", text.lower())
    return " ".join(re.sub(r"[_\-.]+", " ", text).split())


def score(query: str, path: Path) -> float:
    q = _norm(query)
    stem = _norm(path.stem if path.is_file() else path.name)
    if not q or not stem:
        return 0.0
    if stem == q:
        return 1.0
    if q in stem.split() or (len(q) >= 4 and q in stem):
        return 0.9 - min(0.2, (len(stem) - len(q)) / 200)
    words = set(q.split())
    if words and words <= set(stem.split()):
        return 0.85
    return difflib.SequenceMatcher(None, q, stem).ratio()


def find(query: str, roots: list[Path], *, want_folder: bool | None = None) -> list[tuple[float, Path]]:
    """Best matches, best first: [(score, path)]."""
    from nova.system.winops import RUNNABLE

    from nova import ROOT

    nova_root = os.path.normcase(os.path.realpath(ROOT))
    found: list[tuple[float, Path]] = []
    seen = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "$")) and d.lower() not in SKIP_DIRS
                           and depth < MAX_DEPTH
                           and os.path.normcase(os.path.realpath(os.path.join(dirpath, d))) != nova_root]
            if want_folder is not False:
                for d in dirnames:
                    p = Path(dirpath) / d
                    s = score(query, p)
                    if s >= MIN_SCORE:
                        found.append((s + (0.03 if want_folder else 0), p))
            if want_folder is not True:
                for f in filenames:
                    if f.startswith((".", "~$")) or Path(f).suffix.lower().lstrip(".") in RUNNABLE:
                        continue
                    p = Path(dirpath) / f
                    s = score(query, p)
                    if s >= MIN_SCORE:
                        found.append((s, p))
            seen += len(dirnames) + len(filenames)
            if seen > MAX_ENTRIES:
                break
        if seen > MAX_ENTRIES:
            break
    found.sort(key=lambda x: (-x[0], len(str(x[1]))))
    return found[:5]


def _kind(name: str) -> bool | None:
    n = name.lower()
    if re.search(r"\b(folder|directory)\b", n):
        return True
    if re.search(r"\b(file|document|doc|pdf|spreadsheet|presentation|photo|picture|video)\b", n):
        return False
    return None


_cache: dict[str, tuple[float, list[tuple[float, Path]]]] = {}
CACHE_S = 60.0


async def _best(name: str) -> tuple[Path | None, list[Path]]:
    import time

    key = name.lower()
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < CACHE_S:   # the approval question just searched
        hits = hit[1]
    else:
        hits = await asyncio.to_thread(find, name, search_roots(), want_folder=_kind(name))
        _cache.clear()
        _cache[key] = (time.monotonic(), hits)
    if not hits:
        return None, []
    top = hits[0][0]
    close = [p for s, p in hits if top - s <= TIE]
    if len(close) > 1 and len({p.name.lower() for p in close}) > 1:
        return None, close[:3]
    return hits[0][1], []


async def _resolve(ctx: SkillCtx, params: dict) -> dict | None:
    path, _ = await _best(str(params.get("name", "")))
    return {"name": str(path)} if path is not None else None


@skill(
    name="open_file",
    tier=Tier.ACT,
    examples=["open my resume", "open the CSE 576 folder", "open the budget spreadsheet", "show me my downloads"],
    description="Open one of Manav's documents or folders by name (searches his own folders and D:); "
                "never opens programs or scripts.",
    trusts_input=False,
    patterns=[r"(?:open|show me|pull up) (?:my |the )?(?P<name>.+?) (?:file|folder|document|pdf|spreadsheet)"],
    resolve_for_confirm=_resolve,
    confirm_phrase="open {name}",
)
async def open_file(ctx: SkillCtx, name: str) -> Reply:
    from nova.system import winops

    name = " ".join(str(name).split())[:120]
    if not name:
        return Reply(text="Which file or folder?", success=False)
    path, choices = await _best(name)
    if choices:
        listed = "; ".join(f"{p.name} in {p.parent.name}" for p in choices)
        return Reply(text=f"I found a few: {listed}. Which one?", success=False)
    if path is None:
        return Reply(text=f"I couldn't find anything called {name} in your folders.", success=False)
    try:
        await asyncio.to_thread(winops.open_document, str(path), [str(r) for r in search_roots()])
    except Exception as exc:  # noqa: BLE001 -- say why, never guess another file
        _log.warning("open_file refused/failed (%s)", type(exc).__name__)
        return Reply(text=f"I found {path.name} but couldn't open it ({exc}).", success=False)
    where = "folder" if path.is_dir() else path.suffix.lstrip(".").upper() or "file"
    return Reply(text=f"Opened {path.name}" + (f" ({where})." if where else "."))
