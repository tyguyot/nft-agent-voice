# nft-agent-voice

Most NFT agents sound like a chatbot wearing a PFP. This is the setup and the lessons from building one that doesn't: Vibejamin, the character agent for Good Vibes Club, tuned in public on X for months.

It's two things:

1. **`VOICE.md`**, the learnings. How to write a character so it sounds like a person, what to leave out, and every failure we hit with the fix. Useful even if you never run this code.
2. **A working agent.** Replies to mentions on X, writes drafts you approve in Slack, remembers people, knows your lore only when it comes up, and runs on whatever model you want, including cheap open-source ones.

Built by [@tyguyot](https://x.com/tyguyot). MIT licensed.

## The short version of the learnings

- **Personality comes from delivery plus context, not instructions.** Nothing in Vibejamin's prompt says "be funny." He's short, direct, and knows one specific thing about who he's talking to. That's the whole trick.
- **Less prompt, better output.** Every rule you add makes the character more cautious everywhere, not just where you meant it.
- **No example replies in the prompt.** They become templates. Examples cause convergence. Context causes creativity.
- **Character file = how it talks. Context files = what it knows.** Lore loads only when someone brings it up. Otherwise it repeats your project facts in every reply.
- **Every piece of injected info gets a leash.** "Use one thing, ignore the rest." Without it the agent summarizes everything you hand it, and summarizing is what robots do.
- **One model extracts, another performs.** Raw tweets get compressed by a cheap model into 2-3 dry sentences before the character ever sees them.
- **Small models are often better at voice.** Big ones write three paragraphs.
- **Test in private, then go public.** Slack is the rehearsal room. X is the stage.

Full reasoning, numbers, and failure modes in `VOICE.md`.

## Start here

| Read | For |
|---|---|
| `VOICE.md` | Building the character. Start here |
| `SETUP.md` | Keys, Railway, Slack, X, picking models |
| `prompts/character.md` | The annotated character template |
| `presets/gvc/` | Good Vibes Club holders turning their Citizen into an agent |

Fastest way to feel it out, no accounts besides a model key:

```bash
pip install -r requirements.txt
cp .env.example .env          # set AGENT_NAME, a model, and its key
python voice_test.py --models "openrouter:<model-a>,openrouter:<model-b>"
```

That runs `prompts/test-prompts.txt` through each model with your character file and writes a side-by-side comparison.

## How a reply gets built

```
mention in
  -> sanitize (prompt-injection patterns flagged as untrusted)
  -> loop guard (max replies per account per hour)
  -> who is this?           profiles/community.json, max 3 people, one line each
  -> talked before?         last 5 logged interactions, "don't reuse these angles"
  -> topic mentioned?       context/*.md by trigger word, max 2 files
  -> raw tweets / data?     compressed by the cheap PREPROCESS model first
  -> route                  banter -> MODEL_VOICE, real question on a known topic -> MODEL_KNOWLEDGE
  -> generate               character file + every injected block carries its leash
  -> clean                  strip think tags, em-dashes, hashtags, "Name:" prefixes, trim to 280
reply out
```

## What it does

- Replies to @mentions with thread awareness. Remembers every X conversation per person.
- Writes a batch of post drafts twice a day from what your main account posts. Approve, regenerate, or reject in Slack. Nothing posts without a human click.
- "Roast me" using the person's bio and last 5 tweets.
- Looks up NFT traits by token number ("#1234") with a "mention one trait" leash.
- Routes each job to its own model (`provider:model`): Anthropic, OpenRouter, OpenAI, or any OpenAI-compatible server like Ollama.
- Kill switch from Slack. Auto-reply off until you turn it on.

## Files

Flat on purpose. Change the character layer (prompts, context, profiles), not the engine.

| File | Purpose |
|------|---------|
| `prompts/character.md` | How the character talks. The most important file |
| `prompts/roast-mode.md` | Add-on prompt for the /roast endpoint |
| `prompts/test-prompts.txt` | Messages for `voice_test.py` |
| `context/*.md` | What it knows. Each file loads only on its trigger words |
| `profiles/community.json` | One-line descriptions of people it "already knows" |
| `main.py` | FastAPI app: routes, Slack commands, X polling, scheduling |
| `llm_client.py` | Every model call: provider routing, tiers, leashes, output cleanup |
| `context_loader.py` | Trigger matching and the max-files cap |
| `voice_test.py` | Side-by-side model comparison |
| `slack_client.py`, `twitter_client.py` | Platform I/O |
| `memory.py` | SQLite: drafts, profiles, interaction log, token metadata |
| `sanitize.py` | Prompt-injection flagging |
| `rate_limiter.py` | Cost guard on the HTTP endpoints |
| `data_api_client.py`, `wallet_client.py` | Optional live data and read-only wallet |
| `presets/gvc/` | Good Vibes Club preset and guide |

## Building with an AI assistant

Upload the repo plus your character material and paste:

> This repo is a character agent framework. Read VOICE.md and SETUP.md first, then every file. I want to build [NAME]. Here's everything about them: [brain dump: who they are, how they talk, the lore, the community, what they must never do]. Fill in prompts/character.md and the context/ files following VOICE.md. Keep the character file under 90 lines. Don't change the engine. Ask me clarifying questions before writing anything, then walk me through SETUP.md one step at a time.

Voice-to-text the brain dump. It's faster and it sounds more like how you'd describe the character to a friend, which is the point.

## Disclaimer

Running a public agent means it will eventually say something you didn't expect. Test it, keep humans on approval until you trust it, and keep the kill switch handy. You're responsible for what your agent posts.
