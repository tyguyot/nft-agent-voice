"""
Character agent service.

Endpoints:
  POST /draft                - Generate a draft tweet, post to Slack for review (admin key).
  POST /slack/interactions   - Handle Slack button clicks (approve/regen/reject).
  POST /slack/events         - Handle Slack events (@mentions, DMs, thread replies).
  POST /roast                - Generate a roast / read on a person (admin key).
  POST /reflect              - Run a reflection summary (admin key).
  POST /admin/reload-context - Reload context files from disk.
  POST /admin/reload-profiles - Reload profiles from JSON.
  POST /admin/ingest-tweets  - Backfill the source account's history.
  POST /admin/ingest-metadata - Load NFT collection metadata.
  GET  /admin/export-observations - CSV of the interaction log.
  GET  /metadata/{token_id}, POST /metadata/search - Token trait lookups.
  GET  /agent-card           - Serves agent-card.json if present.
  GET  /health               - Health check.
  (all /admin/* and /draft, /roast, /reflect need the X-Admin-Key header)
"""
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler

from dotenv import load_dotenv
load_dotenv()  # local .env; no-op on Railway

import memory
import llm_client
import slack_client
import twitter_client
import context_loader
import data_api_client
from rate_limiter import draft_hourly, draft_daily, reflect_daily


AGENT_NAME = llm_client.AGENT_NAME
# The project's main account. The agent reads it to find things to post about.
SOURCE_HANDLE = os.environ.get("SOURCE_TWITTER_HANDLE", "").lstrip("@")
DAILY_DRAFTS_ENABLED = os.environ.get("DAILY_DRAFTS_ENABLED", "true").lower() == "true"
DRAFT_TIMES_UTC = os.environ.get("DRAFT_TIMES_UTC", "12:00,19:00")
DRAFTS_PER_RUN = int(os.environ.get("DRAFTS_PER_RUN", "4"))
# Slack user IDs (U...) allowed to run reflect, wallet and posting commands.
ADMIN_SLACK_IDS = {x.strip() for x in os.environ.get("ADMIN_SLACK_IDS", "").split(",") if x.strip()}
# Max replies to the same account per hour. Stops agent-to-agent reply loops.
MAX_REPLIES_PER_AUTHOR_PER_HOUR = int(os.environ.get("MAX_REPLIES_PER_AUTHOR_PER_HOUR", "5"))
# NFT collection size for "#1234" token lookups. 0 disables.
COLLECTION_SIZE = int(os.environ.get("COLLECTION_SIZE", "0"))

app = FastAPI(title=f"{AGENT_NAME} Agent")
scheduler = BackgroundScheduler()


def _twitter_configured() -> bool:
    return bool(os.environ.get("TWITTER_BEARER_TOKEN") and os.environ.get("TWITTER_API_KEY"))


@app.on_event("startup")
def on_startup():
    memory.init_db()
    count = memory.load_profiles_from_json()
    if count:
        print(f"[startup] loaded {count} community profiles")

    if not _twitter_configured():
        print("[startup] Twitter credentials missing. Slack-only mode.")
        return

    # Mention polling. Always registered so "go live" in Slack can resume it,
    # but starts paused unless TWITTER_AUTO_REPLY=true.
    poll_interval = int(os.environ.get("TWITTER_POLL_INTERVAL", "90"))
    job_kwargs = {} if twitter_client.AUTO_REPLY_ENABLED else {"next_run_time": None}
    scheduler.add_job(
        _poll_twitter_mentions, "interval", seconds=poll_interval,
        id="twitter_mentions", max_instances=1, **job_kwargs,
    )
    state = "live" if twitter_client.AUTO_REPLY_ENABLED else "paused"
    print(f"[startup] mention polling registered every {poll_interval}s ({state})")

    if SOURCE_HANDLE:
        scheduler.add_job(
            _ingest_source_tweets, "interval", seconds=86400,
            id="source_tweet_ingest", max_instances=1, args=[SOURCE_HANDLE],
        )
        print(f"[startup] daily ingestion of @{SOURCE_HANDLE} scheduled")

    if DAILY_DRAFTS_ENABLED:
        for i, hhmm in enumerate(t.strip() for t in DRAFT_TIMES_UTC.split(",") if t.strip()):
            hour, minute = (int(x) for x in hhmm.split(":"))
            scheduler.add_job(
                _generate_daily_drafts, "cron", hour=hour, minute=minute,
                id=f"daily_drafts_{i}", max_instances=1,
            )
            print(f"[startup] draft batch scheduled {hhmm} UTC")

    scheduler.start()

    if SOURCE_HANDLE:
        _ingest_source_tweets(SOURCE_HANDLE)


@app.on_event("shutdown")
def on_shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ---------- Source account ingestion ----------

def _ingest_source_tweets(handle: str):
    """Fetch recent tweets AND replies from the project account, stored separately."""
    try:
        tweets = twitter_client.get_user_recent_tweets(handle, max_results=5)
        tweet_count = 0
        for tweet_text in tweets:
            if memory.search_observations(tweet_text[:50], hours=48):
                continue
            memory.save_observation(
                source="source_tweets", author=handle, content=tweet_text, channel="twitter",
            )
            tweet_count += 1

        replies = twitter_client.get_user_recent_tweets(
            handle, max_results=5, exclude=["retweets"],
        )
        reply_count = 0
        for reply_text in replies:
            if not reply_text.startswith("@"):
                continue  # original tweet, already captured above
            if memory.search_observations(reply_text[:50], hours=48):
                continue
            memory.save_observation(
                source="source_replies", author=handle, content=reply_text, channel="twitter",
            )
            reply_count += 1

        if tweet_count or reply_count:
            print(f"[twitter] ingested {tweet_count} tweets + {reply_count} replies from @{handle}")
    except Exception as e:
        print(f"[twitter] source ingestion error: {e}")


# ---------- Daily draft generation ----------

def _generate_daily_drafts():
    """
    Generate a batch of standalone post drafts from what the project account has
    been posting. Drafts go to the Slack review channel for approve/regen/reject.
    """
    try:
        recent = memory.get_recent_observations(source="source_tweets", hours=72, limit=10)
        recent_replies = memory.get_recent_observations(source="source_replies", hours=72, limit=10)

        if not recent and not recent_replies:
            print("[drafts] nothing recent from the source account to riff on, skipping")
            return

        topics = ""
        if recent:
            topics = f"what @{SOURCE_HANDLE} has been posting:\n" + "\n".join(
                f"- {obs['content'][:200]}" for obs in recent
            )
        reply_topics = ""
        if recent_replies:
            reply_topics = f"\nwhat @{SOURCE_HANDLE} has been replying to people about:\n" + "\n".join(
                f"- {obs['content'][:200]}" for obs in recent_replies
            )

        n = DRAFTS_PER_RUN
        prompt = (
            f"here's what the @{SOURCE_HANDLE} account has been up to recently:\n\n"
            f"{topics}{reply_topics}\n\n"
            f"write {n} separate posts from you ({AGENT_NAME}). each one is its own "
            f"standalone post, not a reply, not a thread. pick a different topic for "
            f"each. riff on one thing the account posted, add your own angle, or just "
            f"exist in your world. don't repeat an idea. don't reference what day it is. "
            f"each post is 1-2 sentences.\n\n"
            f"output exactly {n} posts, each on its own line, separated by ---"
        )

        injected_context, files_used = context_loader.select_context(
            " ".join(obs["content"][:80] for obs in recent[:3])
        )
        # Saved on each draft so Regenerate rebuilds from the same material.
        regen_prompt = (
            f"write one standalone post from you ({AGENT_NAME}). riff on one thing "
            f"the @{SOURCE_HANDLE} account posted, or just exist in your world. "
            f"1-2 sentences.\n\n{topics}{reply_topics}"
        )

        draft_text, model_used = llm_client.generate_draft(
            context_prompt=prompt, post_type="vibe",
            injected_context=injected_context, fit=False,
        )

        posts = [
            llm_client.fit_to_post(llm_client.clean_output(t))
            for t in draft_text.split("---")
            if t.strip() and len(t.strip()) > 10
        ]
        if not posts:
            print("[drafts] model didn't return parseable drafts")
            return

        count = 0
        for post in posts[:n]:
            draft_id = memory.save_draft(
                context_prompt=regen_prompt, post_type="vibe", draft_text=post,
                model_used=model_used, context_files=",".join(files_used),
            )
            slack_ts = slack_client.post_draft_for_review(
                draft_text=post, draft_id=draft_id, context_prompt="daily draft",
                post_type="vibe", context_files=",".join(files_used), model_used=model_used,
            )
            if slack_ts:
                memory.update_draft(draft_id, slack_ts=slack_ts)
            count += 1

        print(f"[drafts] generated {count} daily drafts")

    except Exception as e:
        print(f"[drafts] draft generation error: {e}")


# ---------- Wallet operations ----------

def _handle_wallet_balance(channel: str, thread_ts: str):
    """Check and report the agent wallet balance."""
    try:
        import wallet_client

        balances = wallet_client.get_eth_balance()
        if not balances:
            slack_client.post_chat_reply(
                channel=channel, text="couldn't check wallet right now",
                thread_ts=thread_ts,
            )
            return

        address = balances.get("address", "unknown")
        mainnet = balances.get("mainnet_eth")
        base = balances.get("base_eth")

        parts = [f"wallet: `{address}`"]
        if mainnet is not None:
            parts.append(f"mainnet: {mainnet:.4f} ETH")
        if base is not None:
            parts.append(f"base: {base:.4f} ETH")

        slack_client.post_chat_reply(
            channel=channel, text="\n".join(parts),
            thread_ts=thread_ts,
        )
    except Exception as e:
        print(f"[wallet] balance handler error: {e}")
        slack_client.post_chat_reply(
            channel=channel, text="wallet check failed, check logs",
            thread_ts=thread_ts,
        )


def _handle_wallet_holdings(channel: str, thread_ts: str):
    """Check and report the agent NFT holdings."""
    try:
        import wallet_client

        nfts = wallet_client.get_nft_holdings()
        if nfts is None:
            slack_client.post_chat_reply(
                channel=channel, text="couldn't check holdings right now",
                thread_ts=thread_ts,
            )
            return

        if not nfts:
            slack_client.post_chat_reply(
                channel=channel, text="wallet is empty. no NFTs yet.",
                thread_ts=thread_ts,
            )
            return

        lines = [f"holding {len(nfts)} NFTs:"]
        for nft in nfts[:20]:  # Cap display at 20
            name = nft.get("name", "Unknown")
            collection = nft.get("collection", "")
            lines.append(f"• {name} ({collection})")

        slack_client.post_chat_reply(
            channel=channel, text="\n".join(lines),
            thread_ts=thread_ts,
        )
    except Exception as e:
        print(f"[wallet] holdings handler error: {e}")
        slack_client.post_chat_reply(
            channel=channel, text="holdings check failed, check logs",
            thread_ts=thread_ts,
        )


def _handle_announce_tx(tx_hash: str, channel: str, thread_ts: str):
    """Look up a transaction and generate a tweet draft announcing it."""
    try:
        import wallet_client

        tx = wallet_client.get_transaction(tx_hash)
        if not tx:
            slack_client.post_chat_reply(
                channel=channel, text="couldn't find that transaction",
                thread_ts=thread_ts,
            )
            return

        # Build context for the character to announce
        tx_summary = (
            f"you just made an onchain transaction.\n"
            f"tx: {tx_hash}\n"
            f"value: {tx['value_eth']:.4f} ETH\n"
            f"status: {tx['status']}\n"
            f"to: {tx['to']}"
        )

        # Generate a tweet draft about it
        injected_context, files_used = context_loader.select_context("wallet transaction onchain")
        tx_prompt = f"announce this transaction in your voice. keep it casual, not technical. {tx_summary}"
        draft_text, model_used = llm_client.generate_draft(
            context_prompt=tx_prompt,
            post_type="vibe",
            injected_context=injected_context,
        )

        draft_id = memory.save_draft(
            context_prompt=tx_prompt,
            post_type="vibe",
            draft_text=draft_text,
            model_used=model_used,
            context_files=",".join(files_used),
        )

        slack_ts = slack_client.post_draft_for_review(
            draft_text=draft_text,
            draft_id=draft_id,
            context_prompt=f"tx announcement: {tx_hash[:10]}...",
            post_type="vibe",
            context_files=",".join(files_used),
            model_used=model_used,
        )
        if slack_ts:
            memory.update_draft(draft_id, slack_ts=slack_ts)

    except Exception as e:
        print(f"[wallet] announce-tx handler error: {e}")
        slack_client.post_chat_reply(
            channel=channel, text="tx announcement failed, check logs",
            thread_ts=thread_ts,
        )


# ---------- Twitter mention polling ----------

def _poll_twitter_mentions():
    """Check for new @mentions and auto-reply."""
    if not twitter_client.AUTO_REPLY_ENABLED:
        return
    try:
        # Load last_mention_id from database (survives restarts)
        last_id = memory.kv_get("last_mention_id")
        if not last_id:
            last_id = None

        mentions = twitter_client.get_mentions(
            since_id=last_id,
            max_results=10,
        )

        if not mentions:
            return

        # Get our own user ID once
        me = twitter_client.get_me()
        if not me:
            return

        # Process oldest first
        mentions.reverse()

        for mention in mentions:
            tweet_id = mention["tweet_id"]

            # Always update the last seen ID (persist to DB)
            memory.kv_set("last_mention_id", tweet_id)

            # Skip if we already replied to this tweet
            if memory.has_replied_to(tweet_id):
                continue

            # Skip our own tweets
            if mention.get("author_id") == me["id"]:
                continue

            # Skip if the agent already directly replied to this specific tweet
            # (catches manual replies from the account too)
            if twitter_client.has_already_replied(tweet_id, me["id"]):
                memory.mark_replied(tweet_id)
                print(f"[twitter] skipping {tweet_id}: already replied to this tweet")
                continue

            author = mention.get("author", {})
            username = author.get("username", "")
            text = mention.get("text", "")

            print(
                f"[twitter] mention from @{username}: {text[:80]!r}"
            )

            # Strip the agent's own @mention and the source account's, but keep
            # other @mentions (they might be the roast target or relevant context)
            clean_text = re.sub(rf'@{re.escape(me["username"])}\b', '', text, flags=re.IGNORECASE).strip()
            if SOURCE_HANDLE:
                clean_text = re.sub(rf'@{re.escape(SOURCE_HANDLE)}\b', '', clean_text, flags=re.IGNORECASE).strip()

            if not clean_text:
                continue

            # Mark as replied BEFORE generating response (prevents double-reply on slow generation)
            memory.mark_replied(tweet_id)

            # Loop guard. Two agents replying to each other will burn money forever.
            # Cap replies to any one account per hour.
            recent_with_author = memory.get_recent_observations(
                source="twitter", author=username, hours=1, limit=MAX_REPLIES_PER_AUTHOR_PER_HOUR,
            )
            if len(recent_with_author) >= MAX_REPLIES_PER_AUTHOR_PER_HOUR:
                print(f"[twitter] loop guard: already replied to @{username} "
                      f"{MAX_REPLIES_PER_AUTHOR_PER_HOUR}x this hour, skipping")
                continue

            # Check if this is a roast request
            # Strip leading @mentions (leftover from tagging others in the tweet)
            roast_text = re.sub(r'^(@\w+\s*)+', '', clean_text).strip()
            roast_match = re.match(r'^(?:can you |please |yo |lol |go |bro )?roast\b\s*(me\b|@?\w*)', roast_text, re.IGNORECASE)

            if roast_match:
                _handle_twitter_roast(mention, roast_match, clean_text)
            else:
                _handle_twitter_reply(mention, clean_text)

    except Exception as e:
        print(f"[twitter] polling error: {e}")


def _handle_twitter_roast(mention: dict, roast_match, clean_text: str):
    """Handle a roast request from Twitter."""
    import sanitize
    clean_text = sanitize.sanitize_for_context(clean_text)

    author = mention.get("author", {})
    username = author.get("username", "")
    target = roast_match.group(1).strip().lstrip("@") if roast_match.group(1) else ""

    # If "roast me" or just "roast", target is the author
    if not target or target.lower() == "me":
        target_username = username
        target_profile = author
    else:
        # Try as a Twitter handle first
        target_username = target
        target_profile = twitter_client.get_user_profile(target) or {}

        # If Twitter lookup failed, check community profiles by nickname
        # (handles cases like "roast sam" where sam is a nickname, not a handle)
        if not target_profile:
            community_profile = memory.get_profile_by_nickname(target)
            if community_profile:
                target_username = community_profile["twitter_handle"]
                target_profile = twitter_client.get_user_profile(target_username) or {}
                print(f"[roast] nickname '{target}' resolved to @{target_username}")

    bio = target_profile.get("bio", "")

    # Fetch their recent tweets for pre-processing
    recent_tweets = []
    if target_username:
        recent_tweets = twitter_client.get_user_recent_tweets(target_username, max_results=5)

    # Pre-process tweets if available
    tweet_summary = ""
    if recent_tweets:
        tweet_summary = llm_client.preprocess_tweets(
            handle=target_username,
            bio=bio,
            tweets=recent_tweets,
        )

    # Check community profiles
    profile_context = ""
    profile = memory.get_profile(target_username)
    if not profile:
        results = memory.search_profiles(target_username)
        if results:
            profile = results[0]
    if profile:
        profile_context = _format_profile_context(profile)

    # --- P0-2: Download images from the mention tweet ---
    images = twitter_client.download_tweet_images(mention["tweet_id"])
    if images:
        print(f"[twitter] downloaded {len(images)} images for roast of @{target_username}")

    # --- P0-3: Query past interactions for memory ---
    interaction_context = ""
    past_interactions = memory.get_recent_observations(
        source="twitter", author=target_username, hours=8760, limit=5,
    )
    if past_interactions:
        lines = [o["content"][:200] for o in past_interactions]
        interaction_context = "your past roasts/interactions with this person (you ALREADY used these angles, do NOT reuse any of them, find completely different material):\n" + "\n".join(
            f"- {line}" for line in lines
        )
    parts = []
    if target_username:
        parts.append(f"handle: @{target_username}")
    if bio:
        parts.append(f"bio: {bio}")
    if tweet_summary:
        parts.append(f"what you noticed from their tweets: {tweet_summary}")

    structured_input = "\n".join(parts)

    # Generate via chat flow (produces better roasts than the /roast endpoint)
    # Include who asked so the model doesn't confuse the requester with the target
    if username.lower() != target_username.lower():
        roast_message = f"@{username} wants you to roast @{target_username}. roast @{target_username}, not @{username}."
    else:
        roast_message = f"roast @{target_username}"
    if tweet_summary:
        roast_message += f"\n\ncontext on the target: {structured_input}"

    # Add interaction memory to injected context
    injected_context = interaction_context if interaction_context else ""

    try:
        reply_text, model_used = llm_client.generate_chat_reply(
            user_message=roast_message,
            profile_context=profile_context,
            injected_context=injected_context,
            images=images,
            platform="twitter",
        )
    except Exception as img_err:
        if images:
            print(f"[twitter] model rejected image for roast, retrying without: {img_err}")
            reply_text, model_used = llm_client.generate_chat_reply(
                user_message=roast_message,
                profile_context=profile_context,
                injected_context=injected_context,
                images=None,
                platform="twitter",
            )
        else:
            raise

    # Post reply
    tweet_id = twitter_client.post_tweet(
        text=reply_text,
        reply_to=mention["tweet_id"],
    )

    if tweet_id:
        print(f"[twitter] replied to @{username} roast: {reply_text[:80]!r}")

        # Save observation for future context
        memory.save_observation(
            source="twitter",
            author=target_username,
            content=f"roasted: {reply_text}",
            channel=f"tweet:{mention['tweet_id']}",
        )
    else:
        print(f"[twitter] failed to reply to @{username}")


def _handle_twitter_reply(mention: dict, clean_text: str):
    """Handle a general @mention (not a roast request)."""
    import sanitize

    # SECURITY: Never process wallet/transaction commands from Twitter
    wallet_triggers = ["send", "transfer", "withdraw", "approve", "sign", "execute", "burn"]
    lower_clean = clean_text.lower().strip()
    if any(lower_clean.startswith(t) for t in wallet_triggers):
        print(f"[security] blocked wallet-like command from Twitter: {clean_text[:80]}")
        # Still reply normally, just don't execute anything
        pass

    # Sanitize the incoming tweet text for prompt injection
    clean_text = sanitize.sanitize_for_context(clean_text)

    author = mention.get("author", {})
    username = author.get("username", "")

    # Look up community profile
    profile_context = ""
    profile = memory.get_profile(username)
    if not profile:
        results = memory.search_profiles(username)
        if results:
            profile = results[0]
    if profile:
        profile_context = _format_profile_context(profile)

    # Always say who it's talking to so it doesn't grab names from thread context.
    # On Twitter, use the twitter handle people recognize. On Slack, use discord handle.
    personalized_message = clean_text
    if profile:
        display_name = profile.get("twitter_handle", username)
        personalized_message = (
            f"(you're talking to @{display_name}. "
            f"address them directly, not in third person. "
            f"don't use their name in every reply, mix it up) {clean_text}"
        )
    else:
        personalized_message = (
            f"(you're talking to @{username}. do not call them by anyone "
            f"else's name from the thread) {clean_text}"
        )

    # --- P0-1: Fetch thread context if this is a reply in a conversation ---
    thread_history = []
    conversation_id = mention.get("conversation_id")
    if conversation_id and conversation_id != mention["tweet_id"]:
        thread_tweets = twitter_client.get_conversation_thread(
            conversation_id, current_tweet_id=mention["tweet_id"],
        )
        if thread_tweets:
            me = twitter_client.get_me()
            my_username = me["username"].lower() if me else ""
            for t in thread_tweets:
                role = "assistant" if t["username"].lower() == my_username else "user"
                t_text = re.sub(r'@\w+', '', t["text"]).strip()
                if t_text:
                    if role == "user":
                        t_text = f"@{t['username']}: {t_text}"
                    thread_history.append({"role": role, "content": t_text})
            print(f"[twitter] loaded {len(thread_history)} messages of thread context")

    # --- P0-2: Download images from the mention tweet ---
    images = twitter_client.download_tweet_images(mention["tweet_id"])
    if images:
        print(f"[twitter] downloaded {len(images)} images from @{username}'s tweet")

    # --- P0-3: Query past interactions for memory ---
    interaction_context = ""
    past_interactions = memory.get_recent_observations(
        source="twitter", author=username, hours=8760, limit=5,
    )
    if past_interactions:
        lines = [o["content"][:200] for o in past_interactions]
        interaction_context = "your past interactions with this person (you ALREADY used these angles and phrases, do NOT reuse any of them, find completely different material):\n" + "\n".join(
            f"- {line}" for line in lines
        )
    # Project tweets that share a real keyword with the message. Stopwords are
    # skipped, otherwise "what"/"this"/"that" match everything and the latest
    # project tweets get injected into every single reply.
    injected_context = ""
    words = [w for w in re.findall(r'\b\w{5,}\b', clean_text.lower()) if w not in _STOPWORDS]
    for word in words:
        obs = memory.search_observations(word, hours=8760, limit=3)
        source_tweets = [o["content"] for o in obs if o["source"] == "source_tweets"]
        if source_tweets:
            injected_context = (
                "something the project account posted that's related "
                "(background only, don't quote it):\n"
                + "\n".join(f"- {t[:200]}" for t in source_tweets[:2])
            )
            break

    # Keyword-triggered context files
    file_context, _ = context_loader.select_context(clean_text)
    if file_context:
        injected_context = (injected_context + "\n\n" + file_context).strip()

    # "#1234" token lookups
    token_context = _token_context(clean_text)
    if token_context:
        injected_context = (token_context + "\n\n" + injected_context).strip()

    # Add interaction memory to injected context
    if interaction_context:
        if injected_context:
            injected_context = interaction_context + "\n\n" + injected_context
        else:
            injected_context = interaction_context

    # Genuine questions about your world go to the knowledge tier.
    # Banter stays on the cheap voice tier.
    tier = _pick_tier(clean_text)

    # Generate reply
    try:
        reply_text, model_used = llm_client.generate_chat_reply(
            user_message=personalized_message,
            thread_history=thread_history,
            profile_context=profile_context,
            injected_context=injected_context,
            images=images,
            platform="twitter",
            tier=tier,
        )
    except Exception as img_err:
        if images:
            print(f"[twitter] model rejected image, retrying without: {img_err}")
            reply_text, model_used = llm_client.generate_chat_reply(
                user_message=personalized_message,
                thread_history=thread_history,
                profile_context=profile_context,
                injected_context=injected_context,
                images=None,
                platform="twitter",
                tier=tier,
            )
        else:
            raise

    # Post reply
    tweet_id = twitter_client.post_tweet(
        text=reply_text,
        reply_to=mention["tweet_id"],
    )

    if tweet_id:
        print(f"[twitter] replied to @{username}: {reply_text[:80]!r}")

        memory.save_observation(
            source="twitter",
            author=username,
            content=f"@{username}: {clean_text} -> you: {reply_text}",
            channel=f"tweet:{mention['tweet_id']}",
        )
    else:
        print(f"[twitter] failed to reply to @{username}")


# ---------- /draft ----------

class DraftRequest(BaseModel):
    context: str
    post_type: str = "general"


@app.post("/draft")
def create_draft(req: DraftRequest, request: Request):
    _verify_admin_key(request)
    allowed, reason = draft_hourly.check_and_record()
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)
    allowed, reason = draft_daily.check_and_record()
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    injected_context, files_used = context_loader.select_context(req.context)
    draft_text, model_used = llm_client.generate_draft(
        req.context, req.post_type, injected_context,
    )

    draft_id = memory.save_draft(
        context_prompt=req.context,
        post_type=req.post_type,
        draft_text=draft_text,
        model_used=model_used,
        context_files=",".join(files_used),
    )

    slack_ts = slack_client.post_draft_for_review(
        draft_text=draft_text,
        draft_id=draft_id,
        context_prompt=req.context,
        post_type=req.post_type,
        context_files=",".join(files_used),
        model_used=model_used,
    )
    if slack_ts:
        memory.update_draft(draft_id, slack_ts=slack_ts)

    return {
        "draft_id": draft_id,
        "draft_text": draft_text,
        "context_files_used": files_used,
        "model_used": model_used,
        "slack_ts": slack_ts,
    }


# ---------- /roast ----------

class RoastRequest(BaseModel):
    """Structured input for the roast / read endpoint."""
    handle: str = ""
    bio: str = ""
    followers: int = 0
    following: int = 0
    joined: str = ""
    traits: str = ""
    extra: str = ""
    tweets: list[str] = []


@app.post("/roast")
def roast(req: RoastRequest, request: Request):
    """Generate a roast / read on a person. Admin key required: every call costs money."""
    _verify_admin_key(request)
    tweet_summary = ""
    if req.tweets:
        tweet_summary = llm_client.preprocess_tweets(
            handle=req.handle,
            bio=req.bio,
            tweets=req.tweets,
        )

    # Build the structured input string
    # Follower counts / join dates are accepted but never passed to the
    # character. Citing numbers makes it sound like a dashboard.
    parts = []
    if req.handle:
        parts.append(f"handle: {req.handle}")
    if req.bio:
        parts.append(f"bio: {req.bio}")
    if req.traits:
        parts.append(f"traits: {req.traits}")
    if tweet_summary:
        parts.append(f"what you noticed from their tweets: {tweet_summary}")
    if req.extra:
        parts.append(f"additional: {req.extra}")

    structured_input = "\n".join(parts)

    # Look up profile for richer context
    profile_context = ""
    if req.handle:
        profile = memory.get_profile(req.handle)
        if profile:
            profile_context = _format_profile_context(profile)

    take_text, model_used = llm_client.generate_roast(
        structured_input=structured_input,
        profile_context=profile_context,
    )

    return {
        "roast": take_text,
        "model_used": model_used,
        "profile_found": bool(profile_context),
    }


# ---------- /slack/interactions ----------

@app.post("/slack/interactions")
async def slack_interactions(
    request: Request,
    background_tasks: BackgroundTasks,
):
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not slack_client.verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    parsed = parse_qs(body.decode("utf-8"))
    payload = json.loads(parsed["payload"][0])

    action = payload["actions"][0]
    action_id = action["action_id"]
    draft_id = int(action["value"])
    user = payload["user"].get("username", "")
    user_id = payload["user"].get("id", "")

    draft = memory.get_draft(draft_id)
    if not draft:
        return {"text": f"Draft {draft_id} not found"}

    if action_id in ("approve", "regenerate", "reject") and user_id not in ADMIN_SLACK_IDS:
        return {"text": "only admins can act on drafts (set ADMIN_SLACK_IDS)"}

    if action_id == "approve":
        if draft.get("status") in ("approved", "posting"):
            return {"text": f"Draft {draft_id} already posted"}
        memory.update_draft(draft_id, status="posting")
        background_tasks.add_task(_handle_approve, draft_id, user)
        return {"text": f"Posting draft {draft_id}..."}
    elif action_id == "regenerate":
        background_tasks.add_task(_handle_regenerate, draft_id, user)
        return {"text": f"Regenerating draft {draft_id}..."}
    elif action_id == "reject":
        memory.update_draft(draft_id, status="rejected", feedback_author=user)
        if draft.get("slack_ts"):
            slack_client.post_status_update(draft["slack_ts"], "❌ Rejected", f"by @{user}")
        return {"text": "Draft rejected"}

    return {"text": "Unknown action"}


def _handle_approve(draft_id: int, user: str):
    draft = memory.get_draft(draft_id)
    if not draft:
        return

    tweet_id = twitter_client.post_tweet(
        draft["draft_text"],
        reply_to=draft.get("reply_to_id") or None,
        quote_tweet_id=draft.get("quote_tweet_id") or None,
    )
    if tweet_id:
        memory.update_draft(
            draft_id,
            status="approved",
            tweet_id=tweet_id,
            posted_at=datetime.now(timezone.utc).isoformat(),
            feedback_author=user,
        )
        if draft.get("slack_ts"):
            slack_client.post_status_update(
                draft["slack_ts"],
                "✅ Posted",
                f"by @{user} | https://twitter.com/i/web/status/{tweet_id}",
            )
    else:
        memory.update_draft(draft_id, status="pending")
        if draft.get("slack_ts"):
            slack_client.post_status_update(
                draft["slack_ts"], "⚠️ Twitter post failed", "check logs",
            )


def _get_rejected_drafts_for_prompt(context_prompt: str) -> list:
    """Collect draft texts that were regenerated or rejected for this same prompt."""
    recent = memory.get_recent_drafts(days=1, limit=200)
    rejected = []
    for d in recent:
        if (d["context_prompt"] == context_prompt
                and d["status"] in ("regenerated", "rejected", "pending")):
            rejected.append(d["draft_text"])
    print(f"[regen] found {len(rejected)} rejected drafts out of {len(recent)} total")
    if rejected:
        for r in rejected:
            print(f"[regen]   avoid: {r[:80]}")
    else:
        print(f"[regen]   no matches for prompt: {context_prompt[:80]}")
    return rejected


def _handle_regenerate(draft_id: int, user: str):
    old_draft = memory.get_draft(draft_id)
    if not old_draft:
        return

    memory.update_draft(draft_id, status="regenerated", feedback_author=user)

    # Collect all rejected/regenerated drafts for this same prompt
    # so the model knows what angles to avoid
    rejected_drafts = _get_rejected_drafts_for_prompt(old_draft["context_prompt"])

    injected_context, files_used = context_loader.select_context(
        old_draft["context_prompt"]
    )
    draft_text, model_used = llm_client.generate_draft(
        old_draft["context_prompt"], old_draft["post_type"], injected_context,
        rejected_drafts=rejected_drafts,
    )

    new_id = memory.save_draft(
        context_prompt=old_draft["context_prompt"],
        post_type=old_draft["post_type"],
        draft_text=draft_text,
        model_used=model_used,
        context_files=",".join(files_used),
        reply_to_id=old_draft.get("reply_to_id") or "",
        quote_tweet_id=old_draft.get("quote_tweet_id") or "",
    )

    slack_ts = slack_client.post_draft_for_review(
        draft_text=draft_text,
        draft_id=new_id,
        context_prompt=old_draft["context_prompt"],
        post_type=old_draft["post_type"],
        context_files=",".join(files_used),
        model_used=model_used,
    )
    if slack_ts:
        memory.update_draft(new_id, slack_ts=slack_ts)


# ---------- /slack/events ----------

_processed_event_ids = set()
_processed_message_ts = set()
_MAX_PROCESSED_IDS = 1000


@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not slack_client.verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    payload = json.loads(body.decode("utf-8"))

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    if payload.get("type") == "event_callback":
        event = payload.get("event", {})
        event_id = payload.get("event_id")

        # Dedup on event_id (Slack retries)
        if event_id and event_id in _processed_event_ids:
            return {"ok": True}
        if event_id:
            _processed_event_ids.add(event_id)
            if len(_processed_event_ids) > _MAX_PROCESSED_IDS:
                _processed_event_ids.clear()

        event_type = event.get("type")

        # Log everything that arrives for debugging
        print(
            f"[events] type={event_type} channel_type={event.get('channel_type')} "
            f"thread_ts={event.get('thread_ts')} user={event.get('user')} "
            f"files={len(event.get('files', []))} "
            f"text={event.get('text', '')[:60]!r}"
        )

        # Dedup on message ts (prevents processing same message twice)
        msg_ts = event.get("ts")
        if msg_ts:
            if msg_ts in _processed_message_ts:
                return {"ok": True}
            _processed_message_ts.add(msg_ts)
            if len(_processed_message_ts) > _MAX_PROCESSED_IDS:
                _processed_message_ts.clear()

        bot_user_id = slack_client.get_bot_user_id()
        if event.get("user") == bot_user_id or event.get("bot_id"):
            return {"ok": True}
        if event.get("subtype") and event.get("subtype") != "file_share":
            return {"ok": True}

        # Handle both app_mention and message events
        # app_mention fires for @mentions, message fires for everything
        # ts dedup ensures we only process once regardless of which arrives
        is_mention = (
            event_type == "app_mention"
            or (bot_user_id and f"<@{bot_user_id}>" in event.get("text", ""))
        )

        if event_type in ("app_mention", "message"):
            if event.get("channel_type") == "im":
                background_tasks.add_task(_handle_slack_chat, event, False)
            elif is_mention and not event.get("thread_ts"):
                background_tasks.add_task(_handle_slack_chat, event, True)
            elif event.get("thread_ts"):
                channel = event.get("channel")
                thread_ts = event.get("thread_ts")
                if is_mention or slack_client.has_bot_posted_in_thread(channel, thread_ts, bot_user_id):
                    background_tasks.add_task(_handle_slack_chat, event, False)

    return {"ok": True}


def _handle_slack_chat(event: dict, is_fresh_summon: bool):
    try:
        channel = event.get("channel")
        user_message = event.get("text", "").strip()
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts") or message_ts

        if not channel:
            return

        bot_user_id = slack_client.get_bot_user_id()
        if bot_user_id:
            user_message = user_message.replace(f"<@{bot_user_id}>", "").strip()

        # Download any images attached to this message
        images = slack_client.download_images_from_event(event)

        # Need either text or images to proceed
        if not user_message and not images:
            return

        # Kill switch: "shut up" / "stop" / "kill" pauses Twitter auto-replies
        # Resume: "go live" / "start" / "resume" re-enables them
        lower_msg = user_message.lower().strip()
        slack_user = event.get("user", "")
        is_admin = slack_user in ADMIN_SLACK_IDS
        if lower_msg in ("shut up", "stop", "kill", "pause", "go dark"):
            twitter_client.AUTO_REPLY_ENABLED = False
            if scheduler.running:
                try:
                    scheduler.pause_job("twitter_mentions")
                except Exception:
                    pass
            slack_client.post_chat_reply(
                channel=channel, text="twitter replies paused. say 'go live' when you want me back.",
                thread_ts=thread_ts,
            )
            return
        elif lower_msg in ("go live", "start", "resume", "unpause", "wake up"):
            if not is_admin:
                slack_client.post_chat_reply(channel=channel, text="only admins can turn me on.", thread_ts=thread_ts)
                return
            twitter_client.AUTO_REPLY_ENABLED = True
            if scheduler.running:
                try:
                    scheduler.resume_job("twitter_mentions")
                except Exception:
                    pass
            slack_client.post_chat_reply(
                channel=channel, text="back on. watching mentions.",
                thread_ts=thread_ts,
            )
            return
        elif lower_msg == "status":
            status = "live" if twitter_client.AUTO_REPLY_ENABLED else "paused"
            slack_client.post_chat_reply(
                channel=channel, text=f"twitter replies: {status}",
                thread_ts=thread_ts,
            )
            return

        # Wallet commands
        # SECURITY: Read-only commands (balance, holdings) are open to anyone in Slack.
        # Anything that could lead to a post or a transaction is admin-only.
        # The agent NEVER takes onchain actions from Twitter.
        if lower_msg in ("wallet", "balance", "check wallet"):
            threading.Thread(target=_handle_wallet_balance, args=(channel, thread_ts), daemon=True).start()
            return

        if lower_msg in ("daily drafts", "generate daily drafts", "drafts"):
            threading.Thread(target=_generate_daily_drafts, daemon=True).start()
            slack_client.post_chat_reply(
                channel=channel, text="generating drafts, check the drafts channel in a sec",
                thread_ts=thread_ts,
            )
            return

        if lower_msg in ("reflect", "/reflect"):
            if not is_admin:
                slack_client.post_chat_reply(
                    channel=channel, text="only admins can run reflect",
                    thread_ts=thread_ts,
                )
                return

            def _run_reflect():
                try:
                    drafts = memory.get_recent_drafts(days=7)
                    corpus_lines = []
                    for d in drafts:
                        status_tag = d["status"].upper()
                        feedback = f" | feedback: {d['feedback']}" if d.get("feedback") else ""
                        corpus_lines.append(
                            f"[{status_tag}] ({d['post_type']}) prompt: {d['context_prompt'][:100]} "
                            f"-> draft: {d['draft_text']}{feedback}"
                        )

                    twitter_obs = memory.get_recent_observations(
                        source="twitter", hours=168, limit=50,
                    )
                    twitter_lines = []
                    for o in twitter_obs:
                        twitter_lines.append(f"[@{o.get('author', '?')}] {o['content'][:200]}")

                    summary = llm_client.run_reflection(
                        drafts_corpus="\n".join(corpus_lines) if corpus_lines else "No drafts this week.",
                        draft_count=len(drafts),
                        days=7,
                        twitter_corpus="\n".join(twitter_lines),
                        twitter_count=len(twitter_obs),
                    )
                    slack_client.post_chat_reply(
                        channel=channel, text=summary, thread_ts=thread_ts,
                    )
                except Exception as e:
                    print(f"[reflect] error: {e}")
                    slack_client.post_chat_reply(
                        channel=channel, text=f"reflect failed: {e}",
                        thread_ts=thread_ts,
                    )

            threading.Thread(target=_run_reflect, daemon=True).start()
            slack_client.post_chat_reply(
                channel=channel, text="reflecting on the last 7 days, give me a sec",
                thread_ts=thread_ts,
            )
            return

        if lower_msg in ("holdings", "nfts", "what do you own"):
            threading.Thread(target=_handle_wallet_holdings, args=(channel, thread_ts), daemon=True).start()
            return

        announce_tx_match = re.match(r'^announce[- ]?tx\s+(0x[a-fA-F0-9]{64})', user_message, re.IGNORECASE)
        if announce_tx_match:
            if not is_admin:
                slack_client.post_chat_reply(
                    channel=channel, text="only admins can run wallet commands.",
                    thread_ts=thread_ts,
                )
                return
            tx_hash = announce_tx_match.group(1)
            threading.Thread(target=_handle_announce_tx, args=(tx_hash, channel, thread_ts), daemon=True).start()
            return

        if lower_msg.startswith("send "):
            slack_client.post_chat_reply(
                channel=channel,
                text="wallet sends aren't wired up here. transactions are done by hand.",
                thread_ts=thread_ts,
            )
            return

        # Manual Twitter reply: "reply [url] [text]" posts exact text
        # "reply [url]" with no text lets the character write its own reply
        reply_match = re.match(
            r'^reply\s+(https?://(?:twitter\.com|x\.com)/\w+/status/(\d+)\S*)\s*(.*)',
            user_message, re.IGNORECASE | re.DOTALL,
        )
        if reply_match and not is_admin:
            slack_client.post_chat_reply(channel=channel, text="only admins can post to twitter.", thread_ts=thread_ts)
            return
        if reply_match:
            tweet_url = reply_match.group(1)
            tweet_id_str = reply_match.group(2)
            reply_text = reply_match.group(3).strip() if reply_match.group(3) else ""

            if reply_text:
                # Post exact text as reply
                posted_id = twitter_client.post_tweet(
                    text=reply_text, reply_to=tweet_id_str,
                )
                if posted_id:
                    memory.mark_replied(tweet_id_str)
                    slack_client.post_chat_reply(
                        channel=channel,
                        text=f"replied: {reply_text}\nhttps://twitter.com/i/web/status/{posted_id}",
                        thread_ts=thread_ts,
                    )
                else:
                    slack_client.post_chat_reply(
                        channel=channel, text="failed to post reply, check logs",
                        thread_ts=thread_ts,
                    )
            else:
                # No text provided: fetch tweet context and generate a reply as a draft
                url_parts = re.search(r'(\w+)/status/', tweet_url)
                tweet_author = url_parts.group(1) if url_parts else ""

                profile_context = ""
                if tweet_author:
                    profile = memory.get_profile(tweet_author)
                    if not profile:
                        results = memory.search_profiles(tweet_author)
                        if results:
                            profile = results[0]
                    if profile:
                        profile_context = _format_profile_context(profile)

                    # Fetch their recent tweets for context
                    user_profile = twitter_client.get_user_profile(tweet_author)
                    bio = user_profile.get("bio", "") if user_profile else ""
                else:
                    bio = ""

                linked = twitter_client.get_tweet(tweet_id_str)
                linked_text = linked["text"] if linked else ""
                reply_prompt = (
                    f"reply to @{tweet_author}'s tweet: \"{linked_text}\""
                    + (f"\ntheir bio: {bio}" if bio else "")
                )
                gen_text, model_used = llm_client.generate_chat_reply(
                    user_message=reply_prompt,
                    profile_context=profile_context,
                    platform="twitter",
                )

                # Post as draft for approval. reply_to_id makes Approve post it as a reply.
                draft_id = memory.save_draft(
                    context_prompt=reply_prompt,
                    post_type="reply",
                    draft_text=gen_text,
                    model_used=model_used,
                    reply_to_id=tweet_id_str,
                )
                slack_ts = slack_client.post_draft_for_review(
                    draft_text=gen_text,
                    draft_id=draft_id,
                    context_prompt=f"reply to @{tweet_author}",
                    post_type="reply",
                )
                if slack_ts:
                    memory.update_draft(draft_id, slack_ts=slack_ts)
                slack_client.post_chat_reply(
                    channel=channel,
                    text=f"draft reply posted to review channel",
                    thread_ts=thread_ts,
                )
            return

        # Post exact text: "post [text]". No model, no draft, straight to Twitter
        post_match = re.match(r'^post\s+(.*)', user_message, re.IGNORECASE | re.DOTALL)
        if post_match and not is_admin:
            slack_client.post_chat_reply(channel=channel, text="only admins can post to twitter.", thread_ts=thread_ts)
            return
        if post_match:
            post_text = post_match.group(1).strip()
            if not post_text:
                slack_client.post_chat_reply(
                    channel=channel, text="need something to post.",
                    thread_ts=thread_ts,
                )
                return

            posted_id = twitter_client.post_tweet(text=post_text)
            if posted_id:
                slack_client.post_chat_reply(
                    channel=channel,
                    text=f"posted: https://twitter.com/i/web/status/{posted_id}",
                    thread_ts=thread_ts,
                )
            else:
                slack_client.post_chat_reply(
                    channel=channel, text="failed to post, check logs",
                    thread_ts=thread_ts,
                )
            return

        # Detect draft/tweet command: "draft [context]" or "tweet [context]"
        draft_match = re.match(r'^(?:draft|tweet|quote)\s+(.*)', user_message, re.IGNORECASE | re.DOTALL)

        if draft_match:
            draft_context = draft_match.group(1).strip()
            if not draft_context:
                slack_client.post_chat_reply(
                    channel=channel, text="need something to work with. what's the tweet about?",
                    thread_ts=thread_ts,
                )
                return

            # Check for Twitter/X URLs in the context
            url_match = re.search(
                r'https?://(?:twitter\.com|x\.com)/(\w+)/status/(\d+)',
                draft_context,
            )

            tweet_context = ""
            reply_to_id = None
            quote_tweet_id = None
            if url_match:
                tweet_author = url_match.group(1)
                tweet_id_str = url_match.group(2)

                # Fetch the SPECIFIC tweet being quoted/replied to
                linked_tweet = twitter_client.get_tweet(tweet_id_str)
                linked_tweet_text = linked_tweet["text"] if linked_tweet else ""

                # Fetch the tweet author's profile and recent tweets
                profile = twitter_client.get_user_profile(tweet_author)
                recent_tweets = twitter_client.get_user_recent_tweets(tweet_author, max_results=5)

                # Build context from the fetched data
                parts = []
                if profile:
                    parts.append(f"@{tweet_author}: {profile.get('bio', '')}")
                if linked_tweet_text:
                    parts.append(f"their tweet: \"{linked_tweet_text}\"")
                if recent_tweets:
                    parts.append("their recent tweets:")
                    for t in recent_tweets[:5]:
                        parts.append(f"  - {t[:200]}")

                tweet_context = "\n".join(parts)

                # Check if this is a quote tweet, reply, or standalone draft inspired by a URL
                is_quote = user_message.lower().startswith("quote")
                is_reply = bool(re.match(r"^(?:draft|tweet)\s+(?:a\s+)?reply\b", user_message.lower()))
                remaining_context = re.sub(
                    r'https?://(?:twitter\.com|x\.com)/\w+/status/\d+\S*',
                    '', draft_context,
                ).strip()

                if is_quote:
                    draft_context = f"quote tweet @{tweet_author}'s post. {remaining_context}"
                    if tweet_context:
                        draft_context += f"\n\ntheir context:\n{tweet_context}"
                    quote_tweet_id = tweet_id_str
                elif is_reply:
                    draft_context = f"reply to @{tweet_author}. {remaining_context}"
                    if tweet_context:
                        draft_context += f"\n\ntheir context:\n{tweet_context}"
                    reply_to_id = tweet_id_str
                else:
                    # "draft a tweet about/similar to [url]": standalone tweet, URL is just context
                    draft_context = f"{remaining_context}"
                    if tweet_context:
                        draft_context += f"\n\nfor reference, @{tweet_author}'s tweet:\n{tweet_context}"

                # Check community profile
                community_profile = memory.get_profile(tweet_author)
                if not community_profile:
                    results = memory.search_profiles(tweet_author)
                    if results:
                        community_profile = results[0]
                if community_profile:
                    draft_context += f"\n\nyou know this person: {community_profile['description']}"

            # Post type comes from the command, not the content.
            # draft/tweet -> knowledge tier (standalone posts get the better model)
            # unless the message names a lighter type like "shitpost".
            post_type = "draft"
            lower_cmd = user_message.lower()
            for pt in ["shitpost", "smart_take", "knowledge", "thread", "lore"]:
                if pt.replace("_", " ") in lower_cmd or pt in lower_cmd:
                    post_type = pt
                    break

            injected_context, files_used = context_loader.select_context(draft_context)
            draft_text, model_used = llm_client.generate_draft(
                draft_context, post_type, injected_context,
            )

            draft_id = memory.save_draft(
                context_prompt=draft_context,
                post_type=post_type,
                draft_text=draft_text,
                model_used=model_used,
                context_files=",".join(files_used),
                reply_to_id=reply_to_id or "",
                quote_tweet_id=quote_tweet_id or "",
            )

            slack_ts = slack_client.post_draft_for_review(
                draft_text=draft_text,
                draft_id=draft_id,
                context_prompt=draft_context[:120],
                post_type=post_type,
                context_files=",".join(files_used),
                model_used=model_used,
            )
            if slack_ts:
                memory.update_draft(draft_id, slack_ts=slack_ts)

            # Confirm in the thread where the command was issued
            slack_client.post_chat_reply(
                channel=channel,
                text=f"draft #{draft_id} posted to the review channel",
                thread_ts=thread_ts,
            )
            return

        thread_history = []
        if event.get("thread_ts"):
            thread_history = slack_client.fetch_thread_history(
                channel=channel,
                thread_ts=event["thread_ts"],
                bot_user_id=bot_user_id,
            )

        channel_context = None
        if is_fresh_summon and event.get("channel_type") != "im":
            channel_context = slack_client.fetch_recent_channel_messages(
                channel=channel, limit=10, exclude_ts=message_ts,
            )

        # --- Live data: fetch from DATA_API_BASE when a trigger phrase appears ---
        matched = data_api_client.match_trigger(user_message)
        if matched:
            data_type, path, params = matched
            print(f"[live_data] triggered: {data_type}")
            raw_data = data_api_client.fetch(path, params)
            if raw_data:
                data_summary = llm_client.preprocess_live_data(data_type=data_type, raw_data=raw_data)
                data_context = (
                    "live data (use only the number that answers the question, "
                    f"say it casually):\n{data_summary}"
                )
                file_context, _ = context_loader.select_context(user_message)
                injected_context = (data_context + "\n\n" + file_context).strip()

                reply_text, _ = llm_client.generate_chat_reply(
                    user_message=user_message,
                    thread_history=thread_history,
                    profile_context=_extract_profile_context(user_message),
                    injected_context=injected_context,
                    platform="slack",
                )
                slack_client.post_chat_reply(channel=channel, text=reply_text, thread_ts=thread_ts)
                return

        # --- Roast detection: fetch live Twitter data for better roasts ---
        roast_match = re.match(r'^(?:can you |please |yo |lol |go )?roast\s+(.+)', user_message, re.IGNORECASE)
        if roast_match:
            target_raw = roast_match.group(1).strip().rstrip("?.!")

            # Extract Twitter handle from URL if provided
            url_match = re.search(r'(?:twitter\.com|x\.com)/(@?\w+)', target_raw)
            if url_match:
                target = url_match.group(1).lstrip("@")
            else:
                # Strip any URLs and use the remaining text as the target name
                target = re.sub(r'https?://\S+', '', target_raw).strip().lstrip("@")

            if not target:
                # URL was provided but no handle extracted
                slack_client.post_chat_reply(
                    channel=channel, text="drop a name or handle, not just a link",
                    thread_ts=thread_ts,
                )
                return

            # Look up community profile: prioritize exact nickname/handle match
            profile = memory.get_profile_by_nickname(target)

            if profile:
                twitter_handle = profile["twitter_handle"]
                profile_context = _format_profile_context(profile)
                print(f"[roast] matched '{target}' → @{twitter_handle}")
            else:
                twitter_handle = target
                profile_context = ""
                print(f"[roast] no profile match for '{target}', using as handle")

            # Fetch Twitter profile and recent tweets for live roast material
            twitter_profile = twitter_client.get_user_profile(twitter_handle)
            bio = twitter_profile.get("bio", "") if twitter_profile else ""
            recent_tweets = twitter_client.get_user_recent_tweets(twitter_handle, max_results=5)

            # If we got nothing from Twitter and no community profile, bail
            if not twitter_profile and not recent_tweets and not profile_context:
                slack_client.post_chat_reply(
                    channel=channel,
                    text=f"can't find @{twitter_handle} on twitter. drop their actual handle and I'll work with that.",
                    thread_ts=thread_ts,
                )
                return

            # Pre-process tweets into roastable material
            tweet_summary = ""
            if recent_tweets:
                tweet_summary = llm_client.preprocess_tweets(
                    handle=twitter_handle,
                    bio=bio,
                    tweets=recent_tweets,
                )

            # Build the roast message with live context
            roast_parts = [f"roast @{twitter_handle}"]
            if bio:
                roast_parts.append(f"bio: {bio}")
            if tweet_summary:
                roast_parts.append(f"what you noticed from their tweets: {tweet_summary}")

            roast_message = "\n".join(roast_parts)

            reply_text, _ = llm_client.generate_chat_reply(
                user_message=roast_message,
                thread_history=thread_history,
                profile_context=profile_context,
                images=images,
            )

            slack_client.post_chat_reply(
                channel=channel, text=reply_text, thread_ts=thread_ts,
            )
            return

        injected_context, _ = context_loader.select_context(user_message)

        token_context = _token_context(user_message)
        if token_context:
            injected_context = (token_context + "\n\n" + injected_context).strip()

        profile_context = _extract_profile_context(user_message)

        # --- Interaction memory: pull past TWITTER interactions with this user ---
        # Slack interactions are NOT saved to the database (internal safe space).
        # But the character can reference what it knows from Twitter interactions.
        slack_user_id = event.get("user", "")
        slack_username = ""
        # Try to match Slack user to a community profile for Twitter-based lookups
        if slack_user_id:
            # Extract any @handles or names from the message to find the person
            profile_matches = _extract_profile_context(user_message)
            if profile_matches:
                # If they're talking about someone, pull that person's Twitter history
                pass
            # Also check if the Slack user themselves has Twitter interactions
            # by looking up their profile via Slack display name
            try:
                slack_info = slack_client.get_user_info(slack_user_id)
                if slack_info:
                    slack_username = slack_info.get("display_name", "") or slack_info.get("real_name", "")
            except Exception:
                pass

            if slack_username:
                # Search observations by their name/handle
                past_interactions = memory.get_recent_observations(
                    source="twitter", author=slack_username, hours=8760, limit=5,
                )
                if not past_interactions:
                    # Try searching by partial match across all twitter observations
                    past_interactions = memory.search_observations(
                        slack_username, hours=8760, limit=5,
                    )
                    past_interactions = [o for o in past_interactions if o["source"] == "twitter"]

                if past_interactions:
                    lines = [o["content"][:200] for o in past_interactions]
                    interaction_context = (
                        "your past twitter interactions that might be relevant:\n"
                        + "\n".join(f"- {line}" for line in lines)
                    )
                    if injected_context:
                        injected_context = interaction_context + "\n\n" + injected_context
                    else:
                        injected_context = interaction_context

        tier = _pick_tier(user_message)

        # Try with images first. If the model rejects the image, retry without.
        try:
            reply_text, model_used = llm_client.generate_chat_reply(
                user_message=user_message,
                thread_history=thread_history,
                channel_context=channel_context,
                injected_context=injected_context,
                profile_context=profile_context,
                images=images,
                tier=tier,
            )
        except Exception as img_err:
            if images:
                print(f"[images] model rejected image, retrying without: {img_err}")
                reply_text, model_used = llm_client.generate_chat_reply(
                    user_message=user_message,
                    thread_history=thread_history,
                    channel_context=channel_context,
                    injected_context=injected_context,
                    profile_context=profile_context,
                    images=None,
                    tier=tier,
                )
            else:
                raise

        # Optional: tag Slack replies with the model so testers know who wrote what.
        if os.environ.get("SHOW_MODEL_IN_SLACK", "false").lower() == "true":
            reply_text += f"\n_{model_used}_"

        slack_client.post_chat_reply(
            channel=channel, text=reply_text, thread_ts=thread_ts,
        )
    except Exception as e:
        print(f"Slack chat handler error: {e}")


# ---------- /reflect ----------

class ReflectRequest(BaseModel):
    days: int = 7


@app.post("/reflect")
def reflect(req: ReflectRequest, request: Request):
    _verify_admin_key(request)
    allowed, reason = reflect_daily.check_and_record()
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    drafts = memory.get_recent_drafts(days=req.days)
    corpus_lines = []
    for d in drafts:
        status_tag = d["status"].upper()
        feedback = f" | feedback: {d['feedback']}" if d.get("feedback") else ""
        corpus_lines.append(
            f"[{status_tag}] ({d['post_type']}) prompt: {d['context_prompt'][:100]} "
            f"-> draft: {d['draft_text']}{feedback}"
        )

    # Pull Twitter observations for the same period
    twitter_obs = memory.get_recent_observations(
        source="twitter", hours=req.days * 24, limit=50,
    )
    twitter_lines = []
    for o in twitter_obs:
        twitter_lines.append(f"[@{o.get('author', '?')}] {o['content'][:200]}")

    summary = llm_client.run_reflection(
        drafts_corpus="\n".join(corpus_lines) if corpus_lines else "No drafts in this period.",
        draft_count=len(drafts),
        days=req.days,
        twitter_corpus="\n".join(twitter_lines),
        twitter_count=len(twitter_obs),
    )
    return {"days": req.days, "summary": summary}


# ---------- Admin ----------

def _verify_admin_key(request):
    """Verify admin API key. Raises 401 if invalid."""
    admin_key = os.environ.get("ADMIN_API_KEY", "")
    if not admin_key:
        # No key configured = admin endpoints disabled for safety
        raise HTTPException(status_code=403, detail="Admin endpoints require ADMIN_API_KEY env var")
    provided = request.headers.get("X-Admin-Key", "")
    if not provided or provided != admin_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")


@app.post("/admin/reload-context")
def reload_context(request: Request):
    _verify_admin_key(request)
    context_loader.reload_context_files()
    return {"status": "reloaded"}


@app.post("/admin/reload-profiles")
def reload_profiles(request: Request):
    _verify_admin_key(request)
    count = memory.load_profiles_from_json()
    return {"status": "reloaded", "profiles_loaded": count}


class IngestRequest(BaseModel):
    handle: str = ""
    max_tweets: int = 3200


@app.post("/admin/ingest-tweets")
def ingest_tweets(req: IngestRequest, request: Request):
    """One-time full history ingest of a Twitter account's tweets."""
    _verify_admin_key(request)
    req.handle = (req.handle or SOURCE_HANDLE).lstrip("@")
    if not req.handle:
        raise HTTPException(status_code=400, detail="No handle given and SOURCE_TWITTER_HANDLE not set")
    tweets = twitter_client.get_user_full_history(
        username=req.handle,
        max_tweets=req.max_tweets,
    )

    if not tweets:
        return {"status": "no tweets found", "handle": req.handle}

    count = 0
    for t in tweets:
        # Deduplicate by checking first 50 chars
        existing = memory.search_observations(t["text"][:50], hours=8760)
        if existing:
            continue
        memory.save_observation(
            source="source_tweets",
            author=req.handle,
            content=t["text"],
            channel=f"twitter:{t.get('created_at', '')}",
        )
        count += 1

    return {
        "status": "ingested",
        "handle": req.handle,
        "tweets_fetched": len(tweets),
        "new_tweets_stored": count,
    }


@app.get("/health")
def health():
    return {"status": "ok", "agent": AGENT_NAME}


@app.get("/agent-card")
def agent_card():
    """Serve an optional agent-card.json (e.g. for ERC-8004 onchain identity)."""
    card_path = Path("./agent-card.json")
    if not card_path.exists():
        raise HTTPException(status_code=404, detail="Agent card not found")
    from fastapi.responses import JSONResponse
    return JSONResponse(content=json.loads(card_path.read_text()))


@app.get("/admin/export-observations")
def export_observations(request: Request, source: str = "", hours: int = 8760):
    """Export observations as CSV. Default: all sources, last year."""
    _verify_admin_key(request)
    import csv
    import io
    from fastapi.responses import StreamingResponse

    observations = memory.get_recent_observations(source=source, hours=hours, limit=99999)

    output = io.StringIO()
    if observations:
        writer = csv.DictWriter(output, fieldnames=observations[0].keys())
        writer.writeheader()
        writer.writerows(observations)

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=observations.csv"},
    )


# ---------- Collection metadata ----------

class MetadataIngestRequest(BaseModel):
    metadata_path: str = os.environ.get("METADATA_PATH", "collection-metadata.json")


@app.post("/admin/ingest-metadata")
def ingest_metadata(req: MetadataIngestRequest, request: Request):
    """
    Ingest NFT collection metadata from one combined JSON file.
    Expected: array of {token_id, name, attributes: [{trait_type, value}], image}.
    Same shape most marketplaces/IPFS metadata use.
    """
    _verify_admin_key(request)

    filepath = req.metadata_path
    if not os.path.exists(filepath):
        return {"status": "file not found", "path": filepath}

    with open(filepath, "r", encoding="utf-8") as f:
        tokens = json.load(f)

    count = 0
    errors = 0
    for data in tokens:
        try:
            token_id = data.get("token_id", 0)
            traits = {
                attr.get("trait_type", "").lower(): attr.get("value", "")
                for attr in data.get("attributes", [])
            }
            memory.upsert_token_metadata(
                token_id=token_id,
                name=data.get("name", f"#{token_id}"),
                traits=traits,
                image_url=data.get("image", ""),
            )
            count += 1
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"[metadata] error processing token: {e}")

    return {
        "status": "ingested",
        "tokens_loaded": count,
        "errors": errors,
        "total_in_db": memory.get_metadata_count(),
    }


@app.get("/metadata/{token_id}")
def get_metadata(token_id: int):
    """Look up token metadata by ID."""
    meta = memory.get_token_metadata(token_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"Token #{token_id} not found")
    return meta


class TraitSearchRequest(BaseModel):
    trait_type: str
    trait_value: str
    limit: int = 10


@app.post("/metadata/search")
def search_metadata(req: TraitSearchRequest):
    """Search for tokens by trait."""
    results = memory.search_tokens_by_trait(req.trait_type, req.trait_value, req.limit)
    return {"results": results, "count": len(results)}


# ---------- Profile helpers ----------

def _format_profile_context(profile: dict) -> str:
    """Format a profile as the character's own memory: natural, not structured."""
    handle = profile.get("twitter_handle", "")
    desc = profile["description"]

    line = f"@{handle}: {desc}"

    extras = []
    if profile.get("tokens"):
        extras.append(f"Holds {profile['tokens']}")
    if profile.get("badges"):
        extras.append(f"Badges: {profile['badges']}")
    if profile.get("tier"):
        extras.append(f"{profile['tier']} tier")
    if profile.get("notes"):
        extras.append(profile["notes"])

    if extras:
        line += " " + ". ".join(extras) + "."

    return line


def _extract_profile_context(text: str) -> str:
    """
    Find community members mentioned in a message.
    Matches @handles AND plain names against twitter and discord handles
    (not descriptions, which caused the wrong people to get pulled in).
    Capped at 3 people so a busy message doesn't flood the prompt.
    """

    profiles_found = []
    seen = set()

    # First pass: @handles (Twitter-style)
    handles = re.findall(r'@(\w+)', text)
    for handle in handles:
        profile = memory.get_profile(handle)
        if profile and profile["twitter_handle"] not in seen:
            seen.add(profile["twitter_handle"])
            profiles_found.append(_format_profile_context(profile))

    # Second pass: search for plain names in the text.
    # Strip the bot mention and common words, then search remaining words individually.
    cleaned = re.sub(r'<@\w+>', '', text).strip()
    stop_words = {"roast", "assess", "check", "look", "at", "who", "is", "tell",
                  "me", "about", "what", "do", "you", "think", "know", "describe",
                  "can", "please", "the", "a", "an", "of", "for", "and", "or",
                  "how", "does", "has", "have", "are", "was", "were", "be", "to",
                  "in", "on", "it", "that", "this", "my", "your", "their", "his",
                  "her", "go", "yo", "lol", "hey", "hi", "gm", "say", "something",
                  "nice", "with"}
    words = [w for w in re.findall(r'\w+', cleaned) if w.lower() not in stop_words and len(w) >= 2]
    for word in words:
        results = memory.search_profiles(word)
        for profile in results:
            if profile["twitter_handle"] not in seen:
                seen.add(profile["twitter_handle"])
                profiles_found.append(_format_profile_context(profile))

    return "\n\n".join(profiles_found[:3])


# ---------- Routing + injection helpers ----------

_STOPWORDS = {
    "about", "after", "again", "being", "could", "doing", "every", "going", "gonna",
    "great", "having", "maybe", "never", "other", "really", "should", "since",
    "still", "thanks", "their", "there", "these", "thing", "things", "think",
    "those", "today", "wanna", "where", "which", "while", "would", "yours",
}

QUESTION_SIGNALS = [
    "what is", "what's", "whats", "how does", "how do", "explain", "tell me about",
    "what are", "who is", "who are", "what happened", "what's happening",
    "what's going on", "what's the deal", "help me understand", "can you explain",
    "what should i know", "where can i", "how can i", "when is", "when does", "?",
]


def _pick_tier(text: str) -> str:
    """Knowledge tier only when it's a genuine question AND it touched a context
    file. Everything else is banter and stays on the cheap voice model."""
    lower = (text or "").lower()
    if any(q in lower for q in QUESTION_SIGNALS) and context_loader.has_topic_match(lower):
        print("[routing] genuine question on a known topic -> knowledge tier")
        return "knowledge"
    return "voice"


def _token_context(text: str) -> str:
    """'#1234' -> one line of traits, with a leash so it doesn't list them all."""
    if not COLLECTION_SIZE:
        return ""
    match = re.search(r'#(\d{1,6})\b', text or "")
    if not match:
        return ""
    token_id = int(match.group(1))
    if not 0 <= token_id <= COLLECTION_SIZE:
        return ""
    meta = memory.get_token_metadata(token_id)
    if not meta or not meta.get("traits"):
        return ""
    traits = ", ".join(f"{k}: {v}" for k, v in meta["traits"].items() if v)
    return (
        f"token #{token_id} traits: {traits}. "
        "mention one trait at most, the rarest or most interesting. never list them."
    )
