"""
All model calls. Drafts, chat replies, roasts, pre-processing, reflection.

Provider-agnostic. Every model is written as "provider:model":

    anthropic:claude-sonnet-5-5
    openrouter:<provider>/<model>        (copy the exact slug from openrouter.ai/models)
    openai:<model>
    custom:<model>                       (any OpenAI-compatible server: Ollama, Together,
                                          Groq, DeepSeek, Fireworks, LM Studio, vLLM...)

Routing is by tier, not by vendor. Set one env var per tier and swap models
without touching code:

    MODEL_VOICE       chat replies, roasts, casual drafts. Should be the cheapest
                      model that holds the voice. Short output, high volume.
    MODEL_KNOWLEDGE   genuine questions that matched a context file, lore posts,
                      standalone drafts. Needs to be accurate, can cost more.
    MODEL_PREPROCESS  dry summarizer for raw tweets / API data. Never performs.
                      Cheapest thing that follows instructions.
    MODEL_REFLECT     weekly review of drafts. Runs rarely.
    MODEL_DEFAULT     used for any tier left blank.
    MODEL_FALLBACK    optional. Retried once if the primary call errors
                      (rate limit, dead endpoint, model can't read images).
"""
import json
import os
import re
import time
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import httpx

# ---------- Config ----------

AGENT_NAME = os.environ.get("AGENT_NAME", "Agent")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "600"))
VOICE_MAX_TOKENS = int(os.environ.get("VOICE_MAX_TOKENS", "300"))
VOICE_TEMPERATURE = float(os.environ.get("VOICE_TEMPERATURE", "0.9"))

# Hard ceiling on anything headed to Twitter/X. 280 for standard accounts.
# Set 0 to disable (premium accounts with long posts).
MAX_POST_CHARS = int(os.environ.get("MAX_POST_CHARS", "280"))

CHARACTER_PATH = Path(os.environ.get("CHARACTER_PATH", "./prompts/character.md"))
ROAST_PATH = Path(os.environ.get("ROAST_PATH", "./prompts/roast-mode.md"))

PROVIDERS = {
    "anthropic": {
        "kind": "anthropic",
        "key_env": "ANTHROPIC_API_KEY",
    },
    "openrouter": {
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
    },
    "openai": {
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
    },
    "custom": {
        "kind": "openai",
        "base_url": os.environ.get("CUSTOM_BASE_URL", "http://localhost:11434/v1"),
        "key_env": "CUSTOM_API_KEY",
    },
}

# post_type -> tier. Anything not listed goes to "voice".
POST_TYPE_TIER = {
    "draft": "knowledge",
    "smart_take": "knowledge",
    "knowledge": "knowledge",
    "thread": "knowledge",
    "lore": "knowledge",
}

# Every block of injected information gets a leash. Info without a constraint
# is how agents end up summarizing everything they were handed.
# Tune the wording here, in one place.
INJECTION_RULES = {
    "relevant_context": (
        "Background you already know. Use at most one detail from it, and only "
        "if it answers what was actually asked. Say it in your own words. Never "
        "recite it or list things from it."
    ),
    "profile": (
        "You already know some of the people in this conversation. This is what "
        "you know. You don't have to use any of it. If something clicks, use one "
        "thing, not several:"
    ),
    "roast_target": "What you know about this person:",
}

_http = httpx.Client(timeout=60)
_anthropic_client = None


def _tier_env(tier: str) -> str:
    return {
        "voice": "MODEL_VOICE",
        "knowledge": "MODEL_KNOWLEDGE",
        "preprocess": "MODEL_PREPROCESS",
        "reflect": "MODEL_REFLECT",
    }.get(tier, "MODEL_DEFAULT")


def resolve_model(tier: str) -> str:
    """Return the "provider:model" spec for a tier."""
    override = os.environ.get("MODEL_OVERRIDE")
    if override:
        return override
    spec = os.environ.get(_tier_env(tier)) or os.environ.get("MODEL_DEFAULT")
    if not spec:
        raise RuntimeError(
            f"No model configured for tier '{tier}'. Set {_tier_env(tier)} "
            f"or MODEL_DEFAULT, e.g. anthropic:claude-sonnet-5-5"
        )
    return spec


def _split_spec(spec: str) -> Tuple[str, str]:
    if ":" not in spec:
        # Bare model name: assume Anthropic for backwards compatibility.
        return ("anthropic", spec)
    provider, model = spec.split(":", 1)
    if provider not in PROVIDERS:
        raise RuntimeError(f"Unknown provider '{provider}' in model spec '{spec}'")
    return (provider, model)


# ---------- Prompt files ----------

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def load_prompt_file(path: Path) -> str:
    """Read a prompt file and strip <!-- comments --> so notes to humans never
    reach the model. Annotate templates freely."""
    text = path.read_text(encoding="utf-8")
    text = _COMMENT_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------- Output cleanup ----------

def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    if text.startswith("“") and text.endswith("”"):
        text = text[1:-1].strip()
    return text


def clean_output(text: str) -> str:
    """Post-process every generated line. Cheap and open models are noisier
    than Claude about this stuff, so the cleanup is deliberately aggressive."""
    # Reasoning models leak their thinking.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = text.strip()
    # "Agent: blah" / "**Agent:** blah"
    text = re.sub(
        rf"^\**\s*{re.escape(AGENT_NAME)}\s*\**\s*:\s*\**", "", text, flags=re.IGNORECASE
    ).strip()
    # "Here's a tweet:" style preambles
    text = re.sub(r"^(here'?s|here is) (a|the|my)[^:\n]{0,40}:\s*", "", text, flags=re.IGNORECASE)
    text = _strip_quotes(text)
    # Markdown doesn't render on Twitter and reads as AI.
    text = text.replace("**", "").replace("__", "")
    # No em/en dashes.
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    # Trailing hashtag runs.
    text = re.sub(r"(\s+#\w+)+\s*$", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def fit_to_post(text: str, limit: int = None) -> str:
    """Trim to the post limit at a sentence boundary, then a word boundary.
    A short sentence beats a tweet that dies mid-thought."""
    limit = MAX_POST_CHARS if limit is None else limit
    if not limit or len(text) <= limit:
        return text
    cut = text[:limit]
    sentence_end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind("\n"))
    if sentence_end >= limit * 0.4:
        return cut[: sentence_end + 1].strip()
    word_end = cut.rfind(" ")
    return (cut[:word_end] if word_end > 0 else cut).rstrip(",;: ").strip()


# ---------- Transport ----------

def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(
            api_key=os.environ.get(PROVIDERS["anthropic"]["key_env"], "")
        )
    return _anthropic_client


def _to_openai_messages(system: str, messages: List[dict]) -> List[dict]:
    """Convert Anthropic-style messages (with base64 image blocks) to OpenAI format."""
    out = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            parts = []
            for block in content:
                if block.get("type") == "image":
                    src = block["source"]
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"},
                    })
                elif block.get("type") == "text":
                    parts.append({"type": "text", "text": block["text"]})
            content = parts
        out.append({"role": m["role"], "content": content})
    return out


def _call_once(spec: str, system: str, messages: List[dict], max_tokens: int,
               temperature: Optional[float]) -> str:
    provider, model = _split_spec(spec)
    cfg = PROVIDERS[provider]
    started = time.time()

    if cfg["kind"] == "anthropic":
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = _anthropic().messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if b.type == "text")
        usage = f"in={resp.usage.input_tokens} out={resp.usage.output_tokens}"
    else:
        key = os.environ.get(cfg["key_env"], "")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if provider == "openrouter":
            headers["X-Title"] = AGENT_NAME
        body = {
            "model": model,
            "messages": _to_openai_messages(system, messages),
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        url = f"{cfg['base_url'].rstrip('/')}/chat/completions"

        resp = None
        for attempt in range(2):
            resp = _http.post(url, headers=headers, json=body)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(2)
                continue
            break
        if resp.status_code != 200:
            raise RuntimeError(f"{spec} HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"{spec} error: {json.dumps(data['error'])[:300]}")
        text = (data["choices"][0]["message"].get("content") or "")
        u = data.get("usage") or {}
        usage = f"in={u.get('prompt_tokens', '?')} out={u.get('completion_tokens', '?')}"

    print(f"[llm] {spec} {usage} {time.time() - started:.1f}s")
    return text


def complete(tier: str, system: str, messages: List[dict], max_tokens: int = None,
             temperature: Optional[float] = None, spec: str = None) -> Tuple[str, str]:
    """Run one completion for a tier. Returns (raw_text, model_spec_used)."""
    spec = spec or resolve_model(tier)
    max_tokens = max_tokens or MAX_TOKENS
    try:
        return (_call_once(spec, system, messages, max_tokens, temperature), spec)
    except Exception as e:
        fallback = os.environ.get("MODEL_FALLBACK")
        if fallback and fallback != spec:
            print(f"[llm] {spec} failed ({e}); retrying on fallback {fallback}")
            try:
                return (_call_once(fallback, system, messages, max_tokens, temperature), fallback)
            except Exception as e2:
                raise RuntimeError(f"primary {spec} failed: {e} | fallback {fallback} failed: {e2}")
        raise


def _with_images(text: str, images: List[Dict[str, str]] = None):
    if not images:
        return text
    blocks = [{
        "type": "image",
        "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]},
    } for i in images]
    blocks.append({"type": "text", "text": text})
    return blocks


# ---------- Draft generation ----------

def generate_draft(
    context_prompt: str,
    post_type: str,
    injected_context: str = "",
    rejected_drafts: List[str] = None,
    fit: bool = True,
) -> Tuple[str, str]:
    """
    Generate a draft post in the character's voice.
    rejected_drafts: earlier attempts for the same prompt. Avoid repeating them.
    fit: trim to MAX_POST_CHARS. Pass False when asking for several posts at once.
    Returns (draft_text, model_used).
    """
    character = load_prompt_file(CHARACTER_PATH)
    tier = POST_TYPE_TIER.get(post_type, "voice")

    user_parts = [
        f"POST TYPE: {post_type}",
        f"CONTEXT: {context_prompt}",
    ]

    if injected_context:
        user_parts.append(
            f"\n<relevant_context>\n{INJECTION_RULES['relevant_context']}\n\n"
            f"{injected_context}\n</relevant_context>"
        )

    if rejected_drafts:
        user_parts.append(
            "\n<rejected_attempts>\nThese were already tried and rejected. "
            "Do NOT repeat these ideas, angles, or phrasing. Go in a completely different direction.\n"
            + "\n".join(f"- {d}" for d in rejected_drafts)
            + "\n</rejected_attempts>"
        )

    user_parts.append(
        "\nWrite the post. Output only the post text. "
        "No preamble. No explanation. No quotes around it. No hashtags."
    )

    raw, model = complete(
        tier, character, [{"role": "user", "content": "\n".join(user_parts)}],
        max_tokens=MAX_TOKENS, temperature=VOICE_TEMPERATURE,
    )
    draft = clean_output(raw)
    return (fit_to_post(draft) if fit else draft, model)


# ---------- Chat reply ----------

def generate_chat_reply(
    user_message: str,
    thread_history: List[Dict[str, str]] = None,
    channel_context: str = None,
    injected_context: str = "",
    profile_context: str = "",
    images: List[Dict[str, str]] = None,
    platform: str = "slack",
    tier: str = "voice",
) -> Tuple[str, str]:
    """
    Conversational reply for Slack or Twitter.
    images: list of {"media_type": "image/png", "data": "<base64>"}
    tier: "voice" by default, "knowledge" for genuine questions that matched context.
    Returns (reply_text, model_used).
    """
    character = load_prompt_file(CHARACTER_PATH)

    if platform == "twitter":
        system = character + (
            "\n\nYou are on Twitter. This is a reply to someone's tweet. "
            "Default to 1-2 sentences. That's your natural length. "
            "Only exception: if someone writes multiple paragraphs of real substance, "
            "you can respond with a short paragraph. Never longer than that. "
            "No 'here's a tweet:' framing. No hashtags. Just respond naturally in your voice. "
            "If thread context is present, it shows the conversation above. "
            "Each message is labeled with a username like '@username: message'. "
            "Pay close attention to which username said which message. "
            "Do NOT attribute a message, emoji, or reaction to the wrong person. "
            "ONLY respond to the final message from the person who tagged you. "
            "Do not respond to things other people said earlier in the thread. "
            "ONLY mention people whose username actually appears in the thread messages. "
            "Do NOT invent that someone is in the thread or said something they didn't. "
            "NEVER use em-dashes. Use periods, commas, or start a new sentence."
        )
    else:
        system = character + (
            "\n\nYou are in Slack. Respond conversationally, not as a tweet. "
            "Keep it natural, short, and in your voice. "
            "NEVER use em-dashes. Use periods, commas, or start a new sentence. "
            "If <channel_context> is present, it is BACKGROUND awareness only. "
            "The conversation that was happening in the channel before you were "
            "summoned. DO NOT respond to messages inside <channel_context>. DO NOT "
            "ask questions about topics in it. DO NOT reference specific things "
            "from it. It exists only so you know the vibe of the room. "
            "Respond ONLY to the current message the user just sent to you."
        )

    if profile_context:
        system += f"\n\n{INJECTION_RULES['profile']}\n\n{profile_context}"

    messages = list(thread_history or [])

    text_parts = []
    if channel_context:
        text_parts.append(f"<channel_context>\n{channel_context.strip()}\n</channel_context>\n")
    text_parts.append(user_message)
    if injected_context:
        text_parts.append(
            f"\n<relevant_context>\n{INJECTION_RULES['relevant_context']}\n\n"
            f"{injected_context}\n</relevant_context>"
        )

    messages.append({"role": "user", "content": _with_images("\n".join(text_parts), images)})

    max_tokens = VOICE_MAX_TOKENS if tier == "voice" else MAX_TOKENS
    raw, model = complete(tier, system, messages, max_tokens=max_tokens,
                          temperature=VOICE_TEMPERATURE)
    reply = clean_output(raw)
    if platform == "twitter":
        reply = fit_to_post(reply)
    return (reply, model)


# ---------- Pre-processing (one model extracts, another performs) ----------

def preprocess_tweets(handle: str, bio: str, tweets: List[str]) -> str:
    """
    Compress a profile + recent tweets into 2-3 factual sentences.
    Runs on the cheap PREPROCESS tier. The performing model never sees raw tweets.
    """
    tweet_block = "\n".join(f"- {t}" for t in tweets[:5])

    prompt = f"""You're preparing notes for someone who is about to talk to this person. Below is their Twitter profile and recent tweets.

Handle: @{handle}
Bio: {bio}

Recent tweets:
{tweet_block}

Your job: pick the 2-3 most interesting, specific, or telling details from everything above. Compress them into a 2-3 sentence summary. Focus on patterns, contradictions, or anything a sharp observer would notice. Do NOT include follower counts or any numbers.

Do NOT be funny yourself. Just surface the material. Write it as brief, factual observations.

Output only the summary. No preamble."""

    raw, _ = complete("preprocess", "", [{"role": "user", "content": prompt}],
                      max_tokens=300, temperature=0.2)
    return clean_output(raw)


def preprocess_live_data(data_type: str, raw_data) -> str:
    """Compress raw API data into 2-3 plain sentences before the character sees it."""
    data_str = json.dumps(raw_data)[:2000]  # cap so one big payload can't flood context

    prompt = f"""You're preparing live data for someone who will mention it casually in conversation. The data type is: "{data_type}"

Raw data:
{data_str}

Rules:
- Compress into 2-3 natural sentences, the way someone who checks this daily would describe it.
- Keep only the numbers someone asking about this would actually want. Round them.
- Don't format as a report or list.
- Do NOT editorialize or add opinions. Just the facts, stated naturally.

Output only the summary. No preamble."""

    raw, _ = complete("preprocess", "", [{"role": "user", "content": prompt}],
                      max_tokens=200, temperature=0.2)
    return clean_output(raw)


# ---------- Roast ----------

def generate_roast(
    structured_input: str,
    profile_context: str = "",
    images: List[Dict[str, str]] = None,
) -> Tuple[str, str]:
    """Roast / read-a-person mode. Character file + roast prompt. Returns (text, model_used)."""
    system = load_prompt_file(CHARACTER_PATH) + "\n\n" + load_prompt_file(ROAST_PATH)

    if profile_context:
        system += (
            f"\n\n<what_you_know_about_this_person>\n{INJECTION_RULES['roast_target']}\n"
            + profile_context
            + "\n</what_you_know_about_this_person>"
        )

    raw, model = complete(
        "voice", system,
        [{"role": "user", "content": _with_images(structured_input, images)}],
        max_tokens=VOICE_MAX_TOKENS, temperature=VOICE_TEMPERATURE,
    )
    return (fit_to_post(clean_output(raw)), model)


# ---------- Reflection ----------

def run_reflection(drafts_corpus: str, draft_count: int, days: int,
                   twitter_corpus: str = "", twitter_count: int = 0) -> str:
    """Review recent drafts and replies. Surface patterns and surgical fixes."""
    twitter_section = ""
    if twitter_corpus:
        twitter_section = f"""

Below are {twitter_count} Twitter interactions from the same period, the agent's actual replies to people:

<twitter_interactions>
{twitter_corpus}
</twitter_interactions>

When analyzing Twitter interactions, look for:
- What topics the agent engages on most
- Whether replies feel natural or repetitive
- Any patterns in how people are engaging
"""

    prompt = f"""You are reviewing {AGENT_NAME}'s recent output to help the team tune the character.

Below are {draft_count} drafts from the last {days} days, with their status and any feedback. Most drafts that aren't approved are simply ignored, not explicitly rejected. Approved drafts represent the team's taste: what passed the bar.

<drafts>
{drafts_corpus}
</drafts>
{twitter_section}
Your job:
1. Identify patterns in what's working (approved drafts, good interactions).
2. Identify patterns in what's not (ignored drafts, flat or repetitive output).
3. Surface repeated angles, phrases, or structures.
4. Propose specific, surgical edits. Prefer removing a line from the character file, or moving knowledge into a context file, over adding new rules.

Do not rewrite the character file. Do not write code. Be direct and specific. Under 500 words."""

    raw, _ = complete("reflect", "", [{"role": "user", "content": prompt}], max_tokens=2000)
    return raw.strip()
