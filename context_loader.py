"""
Keyword-triggered context injection. Decides which context/*.md files go into
a call based on the message text.

Rules that keep the agent from sounding like a wiki:
- Triggers match whole words only ("art" does not match "party").
- At most MAX_CONTEXT_FILES files per call, ranked by trigger hits.
- A file with no triggers is ALWAYS injected. Only do that on purpose.
- <!-- comments --> are stripped, so annotate files for humans freely.
"""
import os
import re
from pathlib import Path
from typing import List, Tuple

CONTEXT_DIR = Path(os.environ.get("CONTEXT_DIR", "./context"))
MAX_CONTEXT_FILES = int(os.environ.get("MAX_CONTEXT_FILES", "2"))

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _parse_frontmatter(content: str) -> Tuple[List[str], str]:
    if not content.startswith("---"):
        return ([], content)

    match = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
    if not match:
        return ([], content)

    frontmatter, body = match.group(1), match.group(2)
    triggers = []
    for line in frontmatter.split("\n"):
        if line.strip().startswith("triggers:"):
            raw = line.split(":", 1)[1].strip()
            triggers = [t.strip().lower() for t in raw.split(",") if t.strip()]

    return (triggers, body.strip())


def _trigger_regex(trigger: str) -> re.Pattern:
    # whole word, plus simple suffixes: "whitelist" matches "whitelisted", "riddles"
    return re.compile(r"(?<!\w)" + re.escape(trigger) + r"(?:s|es|ed|ing)?(?!\w)")


def load_context_files() -> List[dict]:
    if not CONTEXT_DIR.exists():
        return []

    files = []
    for path in sorted(CONTEXT_DIR.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        triggers, body = _parse_frontmatter(content)
        body = re.sub(r"\n{3,}", "\n\n", _COMMENT_RE.sub("", body)).strip()
        files.append({
            "name": path.stem,
            "triggers": triggers,
            "patterns": [_trigger_regex(t) for t in triggers],
            "body": body,
        })
    return files


_CONTEXT_FILES = load_context_files()


def select_context(prompt: str) -> Tuple[str, List[str]]:
    """Return (context_text, file_names) for files whose triggers appear in prompt."""
    prompt_lower = (prompt or "").lower()
    always, scored = [], []

    for ctx in _CONTEXT_FILES:
        if not ctx["triggers"]:
            always.append(ctx)
            continue
        hits = sum(1 for p in ctx["patterns"] if p.search(prompt_lower))
        if hits:
            scored.append((hits, ctx))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    selected = always + [ctx for _, ctx in scored[:MAX_CONTEXT_FILES]]

    if not selected:
        return ("", [])

    body = "\n\n".join(
        f"### Context: {ctx['name']}\n\n{ctx['body']}" for ctx in selected
    )
    return (body, [ctx["name"] for ctx in selected])


def has_topic_match(prompt: str) -> bool:
    """True if a triggered (not always-on) context file matched. Used for routing
    genuine questions to the knowledge tier."""
    prompt_lower = (prompt or "").lower()
    return any(
        p.search(prompt_lower)
        for ctx in _CONTEXT_FILES if ctx["triggers"]
        for p in ctx["patterns"]
    )


def reload_context_files():
    global _CONTEXT_FILES
    _CONTEXT_FILES = load_context_files()
