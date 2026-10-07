"""
Side-by-side voice test. Runs the same messages through several models with
your character file + context files, so you can compare voice and cost before
deploying anything. No Slack, no Twitter, no database.

    python voice_test.py --models "anthropic:claude-haiku-4-5-20251001,openrouter:<provider>/<model>"
    python voice_test.py --models "$MODEL_VOICE" --platform slack
    python voice_test.py --models "a,b,c" --prompts prompts/test-prompts.txt --runs 2

Writes voice-test-results.md. Read it out loud. The model that sounds like a
person in the fewest words wins the voice tier.
"""
import argparse
import json
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # local .env; no-op on Railway

import llm_client
import context_loader


def load_profiles(path: Path) -> dict:
    if not path.exists():
        return {}
    return {p["twitter_handle"].lower(): p for p in json.loads(path.read_text())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True, help="comma-separated provider:model specs")
    ap.add_argument("--prompts", default="prompts/test-prompts.txt")
    ap.add_argument("--platform", default="twitter", choices=["twitter", "slack"])
    ap.add_argument("--runs", type=int, default=1, help="samples per prompt per model")
    ap.add_argument("--out", default="voice-test-results.md")
    args = ap.parse_args()

    specs = [m.strip() for m in args.models.split(",") if m.strip()]
    prompts = [
        line.strip() for line in Path(args.prompts).read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    profiles = load_profiles(Path("profiles/community.json"))

    lines = [f"# Voice test ({args.platform})\n", f"Models: {', '.join(specs)}\n"]
    totals = {s: {"chars": 0, "n": 0, "secs": 0.0, "errors": 0} for s in specs}

    for msg in prompts:
        profile_context, handle = "", ""
        if msg.startswith("@") and ":" in msg:
            handle, msg = msg[1:].split(":", 1)
            msg = msg.strip()
            p = profiles.get(handle.lower())
            if p:
                profile_context = f"@{p['twitter_handle']}: {p['description']}"
        injected, files = context_loader.select_context(msg)

        lines.append(f"\n## {('@' + handle + ': ') if handle else ''}{msg}\n")
        lines.append(f"_context: {', '.join(files) or 'none'} | profile: {'yes' if profile_context else 'no'}_\n")

        for spec in specs:
            for _ in range(args.runs):
                started = time.time()
                try:
                    reply = _run(spec, msg, injected, profile_context, args.platform)
                    secs = time.time() - started
                    t = totals[spec]
                    t["chars"] += len(reply); t["n"] += 1; t["secs"] += secs
                    lines.append(f"- **{spec.split('/')[-1]}** ({len(reply)} chars, {secs:.1f}s): {reply}")
                except Exception as e:
                    totals[spec]["errors"] += 1
                    lines.append(f"- **{spec.split('/')[-1]}**: ERROR {str(e)[:200]}")
        print(f"done: {msg[:60]}")

    lines.append("\n## Summary\n")
    lines.append("| model | avg chars | avg secs | errors |")
    lines.append("|---|---|---|---|")
    for spec, t in totals.items():
        avg_c = t["chars"] / t["n"] if t["n"] else 0
        avg_s = t["secs"] / t["n"] if t["n"] else 0
        lines.append(f"| {spec} | {avg_c:.0f} | {avg_s:.1f} | {t['errors']} |")

    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"\nwrote {args.out}")


def _run(spec, msg, injected, profile_context, platform):
    """Same path the live agent uses, pinned to one model, fallback off so a
    failing model shows up as an error instead of hiding behind the fallback."""
    import os
    previous = os.environ.get("MODEL_OVERRIDE")
    previous_fb = os.environ.pop("MODEL_FALLBACK", None)
    os.environ["MODEL_OVERRIDE"] = spec
    try:
        reply, _ = llm_client.generate_chat_reply(
            user_message=msg,
            injected_context=injected,
            profile_context=profile_context,
            platform=platform,
            tier="voice",
        )
        return reply
    finally:
        if previous_fb is not None:
            os.environ["MODEL_FALLBACK"] = previous_fb
        if previous is None:
            os.environ.pop("MODEL_OVERRIDE", None)
        else:
            os.environ["MODEL_OVERRIDE"] = previous


if __name__ == "__main__":
    main()
