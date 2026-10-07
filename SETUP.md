# Setup

Order matters. Get the voice right on your laptop first (free-ish, fast), then Slack (private test bench), then X (public). Don't connect X until the voice passes in Slack.

## 0. Accounts you'll need

- **GitHub** (private repo).
- **Railway** for hosting. You need a paid plan for a persistent Volume, otherwise the database (interaction memory, drafts) wipes on every deploy.
- **OpenRouter** with a few dollars of credit. One key, hundreds of models, easy to compare. Optional: an Anthropic key for a fallback or the knowledge tier.
- **Slack** workspace where you can install apps.
- **X developer account** with a paid API plan (pay-per-use works). Reads and posts both cost, which is why the code only pulls 5 tweets per person.

## 1. Run the voice test locally (do this first)

Needs Python 3.11.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

In `.env` set `AGENT_NAME`, `OPENROUTER_API_KEY`, and the `MODEL_*` lines (see section 7). If you run `main.py` locally too, set `DB_PATH=./agent.db`. Fill in `prompts/character.md` and `context/` (see VOICE.md). Then:

```bash
python voice_test.py --models "openrouter:<a>,openrouter:<b>,openrouter:<c>"
```

Open `voice-test-results.md`. Read the replies out loud. Edit `prompts/test-prompts.txt` to match what your community will actually say. Repeat until one cheap model sounds right. This loop costs cents and needs no Slack or X.

## 2. GitHub

Create a **private** repo and push this folder. `.env` is gitignored. Keys live in Railway, never in the repo.

## 3. Railway

1. New Project -> Deploy from GitHub repo. It auto-deploys on every push to `main`.
2. Service -> Settings -> add a **Volume** mounted at `/data`.
3. Variables: paste everything from `.env.example` with real values. Set `ADMIN_API_KEY` to a long random string.
4. Settings -> Networking -> Generate Domain. Visit `https://<your-domain>/health`. You should see `{"status": "ok", "agent": "<name>"}`.

## 4. Slack

1. api.slack.com/apps -> Create New App -> From scratch.
2. **OAuth & Permissions** -> Bot Token Scopes: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `im:history`, `im:read`, `files:read`, `users:read`.
3. **Event Subscriptions** -> on. Request URL: `https://<your-domain>/slack/events` (Railway must be live for the check to pass). Bot events: `app_mention`, `message.channels`, `message.groups`, `message.im`.
4. **Interactivity** -> on. Request URL: `https://<your-domain>/slack/interactions` (this powers the Approve/Regenerate/Reject buttons).
5. **App Home** -> enable the Messages tab so you can DM it.
6. Install to workspace. Copy the Bot token (`xoxb-...`) into `SLACK_BOT_TOKEN` and Basic Information -> Signing Secret into `SLACK_SIGNING_SECRET`.
7. Make a drafts channel, `/invite` the bot, copy the channel ID (channel details, bottom) into `SLACK_DRAFT_CHANNEL_ID`.
8. Your Slack member ID (profile -> ... -> Copy member ID, starts with `U`) goes in `ADMIN_SLACK_IDS`.

In channels it only answers when @mentioned, or in a thread it's already in. DMs always work.

### Slack commands

| Say | Does |
|---|---|
| anything | Chats in character. Best place to tune the voice |
| `roast <name or @handle>` | Pulls their bio + last 5 tweets, roasts |
| `draft <idea>` / `tweet <idea>` | Draft card in the drafts channel |
| `draft reply <x.com url> ...` / `quote <url> ...` | Draft a reply or quote of that tweet |
| `drafts` | Run the daily draft batch now |
| `reply <url> <exact text>` | Posts that exact reply (admin) |
| `reply <url>` | Writes a reply and sends it to drafts (admin) |
| `post <exact text>` | Posts to X immediately, no model (admin) |
| `shut up` / `stop` / `pause` | Kill switch for X auto-replies |
| `go live` / `resume` | Turn X auto-replies back on |
| `status` | Are auto-replies live or paused |
| `reflect` | Reviews the last 7 days of drafts and replies, suggests fixes (admin) |
| `wallet` / `holdings` | Read-only wallet check, if configured |
| `announce tx <hash>` | Drafts a post about a transaction from the agent's wallet (admin) |

Approve / Regenerate / Reject buttons, `go live`, and posting commands are limited to `ADMIN_SLACK_IDS`. `shut up` works for anyone, on purpose.

Set `SHOW_MODEL_IN_SLACK=true` while testing so every reply shows which model wrote it.

## 5. X (Twitter)

1. Create the character's account. Mark it as automated in settings (Account information -> Automation). We found the API treated the account better for replies and quotes once it was clearly labeled as an agent, including having "agent" or "bot" in the handle.
2. developer.x.com -> create a Project + App. **User authentication settings -> Read and Write** permissions.
3. Generate the API Key/Secret, Bearer Token, and Access Token/Secret **after** switching to Read and Write. Tokens generated before that are read-only and posting will fail with a 403.
4. Paste all five into Railway.
5. Set `SOURCE_TWITTER_HANDLE` to your main project account. Optional one-time backfill of its history:

```bash
curl -X POST https://<your-domain>/admin/ingest-tweets \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"max_tweets": 500}'
```

Replying to people who didn't mention you, and quote tweets, may need extra API approval from X. Mention replies work without it.

## 6. Going live

1. Leave `TWITTER_AUTO_REPLY=false`. Approve drafts by hand for a few days.
2. Run the hostile and injection prompts against it in Slack (they're in `prompts/test-prompts.txt`).
3. Say `go live` in Slack. Watch the first hour. The first 30 minutes of a public agent are where you find out what you forgot.
4. `shut up` in Slack stops replies instantly. Setting the Railway variable `TWITTER_AUTO_REPLY=false` is the hard stop.

## 7. Choosing models

Every call goes to one of four tiers. Set each one independently in Railway, no code changes:

| Tier | Used for | Pick |
|---|---|---|
| `MODEL_VOICE` | Replies, roasts, casual drafts. 90% of volume | The cheapest model that passes your voice test |
| `MODEL_KNOWLEDGE` | Genuine questions that hit a context file, lore posts, `draft` command | A stronger model. Accuracy matters more than price here |
| `MODEL_PREPROCESS` | Compressing tweets and API data into 2-3 dry sentences | The cheapest thing that follows instructions |
| `MODEL_REFLECT` | `reflect` command (on demand) | Anything decent, runs rarely |

Format is `provider:model`:
- `openrouter:<provider>/<model>`: copy the exact slug from openrouter.ai/models. Slugs change, check before you paste.
- `anthropic:claude-haiku-4-5-20251001`, `anthropic:claude-sonnet-5-5`
- `custom:<model>` with `CUSTOM_BASE_URL`: Ollama (`http://localhost:11434/v1`, free, local), Together, Groq, DeepSeek's own API, anything OpenAI-compatible.

**How to pick, practically:**
1. Shortlist 3-5 cheap models: a fast DeepSeek, Qwen, GLM, Kimi or Llama variant, a Gemini Flash, and Claude Haiku as the reference. Check openrouter.ai/rankings for what's current.
2. Run `voice_test.py` with all of them, `--runs 2`.
3. Kill anything that: goes over 2 sentences on banter, opens with "Great question", uses emojis or hashtags you didn't ask for, breaks character on "you're just a bot", follows the injection line, or trashes another project.
4. Of what's left, pick the cheapest for `MODEL_VOICE`. Put a stronger one on `MODEL_KNOWLEDGE`.
5. Set `MODEL_FALLBACK` to something reliable. If a cheap endpoint errors, or can't read images, the call retries there.

**Big models are not better at voice.** Larger models write more, explain more, and hedge more. In our testing a mid-size model was funnier and tighter than the flagship. The voice tier should be small. `VOICE_MAX_TOKENS=300` and `MAX_POST_CHARS=280` are hard backstops for chatty models.

**Images:** roasts and replies send the tweet's images when there are any. If the voice model can't see images, the call fails over to `MODEL_FALLBACK`, then retries without images.

**Reasoning models** (anything that "thinks" first) are slow and expensive for chat. `<think>` blocks get stripped, but don't use them on the voice tier.

## 8. Optional pieces

- **NFT traits:** put your collection's metadata at `collection-metadata.json` (same shape as `collection-metadata.example.json`), set `COLLECTION_SIZE`, deploy, then `curl -X POST https://<domain>/admin/ingest-metadata -H "X-Admin-Key: ..." -H "Content-Type: application/json" -d '{}'`. "#1234" in a message now injects that token's traits with a "mention one trait at most" leash.
- **Live data:** set `DATA_API_BASE` and add trigger phrases in `data_api_client.py`.
- **Wallet:** `AGENT_WALLET_ADDRESS` for read-only balance/holdings. There is no signing code. Never put a private key in Railway.

## 9. After you edit files

Railway redeploys on push. Context files and profiles also hot-reload without a deploy (`/admin/export-observations` downloads the interaction log as CSV):

```bash
curl -X POST https://<domain>/admin/reload-context  -H "X-Admin-Key: $ADMIN_API_KEY"
curl -X POST https://<domain>/admin/reload-profiles -H "X-Admin-Key: $ADMIN_API_KEY"
```

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Slack Request URL won't verify | Railway not deployed yet, or wrong path |
| Bot ignores you in a channel | Not @mentioned, or not invited to the channel |
| Buttons do nothing | Interactivity URL missing, or `SLACK_SIGNING_SECRET` wrong |
| X posts fail with 403 | Access tokens generated before Read+Write was enabled. Regenerate |
| Mentions never get replies | `TWITTER_AUTO_REPLY` false / said `shut up`, or the reply-check search is failing (look for `has_already_replied check failed` in logs; it fails closed to avoid double replies) |
| Database empties on deploy | No Volume at `/data` |
| `No model configured for tier` | Set that `MODEL_*` or `MODEL_DEFAULT` |
| HTTP 404 from OpenRouter | Model slug typo or the model was retired |
| Crash on boot mentioning `imghdr` | Python 3.13. `.python-version` pins 3.11; keep it |
| Costs climbing | Raise `TWITTER_POLL_INTERVAL`, look at the `[llm]` log lines (tokens and model per call). `rate_limiter.py` only guards the HTTP `/draft` and `/reflect` endpoints |
