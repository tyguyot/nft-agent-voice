"""
Twitter / X client. Posting, reading mentions, fetching user profiles.
Uses tweepy with OAuth 1.0a User Context.
"""
import os
from typing import Optional, List, Dict

import tweepy

TWITTER_API_KEY = os.environ.get("TWITTER_API_KEY", "")
TWITTER_API_SECRET = os.environ.get("TWITTER_API_SECRET", "")
TWITTER_ACCESS_TOKEN = os.environ.get("TWITTER_ACCESS_TOKEN", "")
TWITTER_ACCESS_TOKEN_SECRET = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET", "")
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN", "")

# Auto-reply to mentions. OFF by default: test the voice in Slack first,
# then set TWITTER_AUTO_REPLY=true (or say "go live" to the bot in Slack).
AUTO_REPLY_ENABLED = os.environ.get("TWITTER_AUTO_REPLY", "false").lower() == "true"


def _get_client() -> tweepy.Client:
    return tweepy.Client(
        bearer_token=TWITTER_BEARER_TOKEN,
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_TOKEN_SECRET,
        wait_on_rate_limit=True,
    )


# ---------- Posting ----------

def post_tweet(text: str, reply_to: str = None, quote_tweet_id: str = None) -> Optional[str]:
    """Post a tweet. Optionally reply to or quote a tweet ID. Returns tweet ID on success."""
    import llm_client
    if llm_client.MAX_POST_CHARS and len(text) > llm_client.MAX_POST_CHARS:
        print(f"[twitter] post over {llm_client.MAX_POST_CHARS} chars, trimming: {text[:80]!r}")
        text = llm_client.fit_to_post(text)
    try:
        client = _get_client()
        kwargs = {"text": text}
        if reply_to:
            kwargs["in_reply_to_tweet_id"] = reply_to
        if quote_tweet_id:
            kwargs["quote_tweet_id"] = quote_tweet_id
        response = client.create_tweet(**kwargs)
        return str(response.data["id"])
    except Exception as e:
        print(f"[twitter] post failed: {e}")
        return None


def has_already_replied(tweet_id: str, my_user_id: str = None) -> bool:
    """
    Check if the tweet being replied to was already answered by the agent.
    Only returns True if the agent's reply is a DIRECT reply to this specific tweet.
    Allows back-and-forth conversation: if someone replies after the agent,
    that's a new message that gets a response.
    """
    try:
        client = _get_client()
        if not my_user_id:
            me = get_me()
            if not me:
                return False
            my_user_id = me["id"]

        # Search for our replies to this specific tweet
        response = client.search_recent_tweets(
            query=f"in_reply_to_tweet_id:{tweet_id} from:{(get_me() or {}).get('username', '')}",
            max_results=10,
        )

        if response.data and len(response.data) > 0:
            return True
        return False

    except Exception as e:
        print(f"[twitter] has_already_replied check failed: {e}")
        # If the check fails, err on the side of not replying
        return True


# ---------- Reading mentions ----------

_me_cache: Optional[dict] = None


def get_me() -> Optional[dict]:
    """Get the authenticated user's info. Cached: it never changes, and every
    X API read costs money on pay-per-use plans."""
    global _me_cache
    if _me_cache:
        return _me_cache
    try:
        client = _get_client()
        response = client.get_me(user_fields=["id", "username", "name"])
        if response.data:
            _me_cache = {
                "id": str(response.data.id),
                "username": response.data.username,
                "name": response.data.name,
            }
            return _me_cache
    except Exception as e:
        print(f"[twitter] get_me failed: {e}")
    return None


def get_mentions(since_id: str = None, max_results: int = 10) -> List[dict]:
    """
    Fetch recent mentions of the authenticated user.
    Returns list of tweet dicts with author info.
    """
    try:
        client = _get_client()
        me = get_me()
        if not me:
            return []

        kwargs = {
            "id": me["id"],
            "max_results": min(max_results, 100),
            "tweet_fields": [
                "id", "text", "author_id", "conversation_id",
                "in_reply_to_user_id", "created_at",
            ],
            "expansions": ["author_id"],
            "user_fields": ["id", "username", "name", "description",
                           "public_metrics", "profile_image_url"],
        }
        if since_id:
            kwargs["since_id"] = since_id

        response = client.get_users_mentions(**kwargs)

        if not response.data:
            return []

        # Build author lookup from includes
        authors = {}
        if response.includes and "users" in response.includes:
            for user in response.includes["users"]:
                authors[str(user.id)] = {
                    "id": str(user.id),
                    "username": user.username,
                    "name": user.name,
                    "bio": user.description or "",
                    "followers": user.public_metrics.get("followers_count", 0) if user.public_metrics else 0,
                    "following": user.public_metrics.get("following_count", 0) if user.public_metrics else 0,
                    "profile_image_url": user.profile_image_url or "",
                }

        mentions = []
        for tweet in response.data:
            author_id = str(tweet.author_id)
            mentions.append({
                "tweet_id": str(tweet.id),
                "text": tweet.text,
                "author_id": author_id,
                "author": authors.get(author_id, {}),
                "conversation_id": str(tweet.conversation_id) if tweet.conversation_id else None,
                "in_reply_to_user_id": str(tweet.in_reply_to_user_id) if tweet.in_reply_to_user_id else None,
                "created_at": str(tweet.created_at) if tweet.created_at else None,
            })

        return mentions

    except Exception as e:
        print(f"[twitter] get_mentions failed: {e}")
        return []


# ---------- Fetching user tweets ----------

def get_user_recent_tweets(username: str, max_results: int = 5, exclude: list = None) -> List[str]:
    """
    Fetch a user's recent tweets by username.
    Returns list of tweet text strings.
    exclude: list of tweet types to exclude. Default: ["replies", "retweets"]
    """
    if exclude is None:
        exclude = ["replies", "retweets"]
    try:
        client = _get_client()

        # Look up user by username
        user_resp = client.get_user(
            username=username,
            user_fields=["id"],
        )
        if not user_resp.data:
            return []

        user_id = user_resp.data.id

        # Get their recent tweets with specified exclusions
        kwargs = {
            "id": user_id,
            "max_results": min(max_results, 100),
            "tweet_fields": ["text"],
        }
        if exclude:
            kwargs["exclude"] = exclude

        tweets_resp = client.get_users_tweets(**kwargs)

        if not tweets_resp.data:
            return []

        return [tweet.text for tweet in tweets_resp.data]

    except Exception as e:
        print(f"[twitter] get_user_recent_tweets failed: {e}")
        return []


def get_user_full_history(username: str, max_tweets: int = 3200) -> List[dict]:
    """
    Fetch a user's full tweet history with pagination.
    Returns list of dicts with text and created_at.
    Max ~3200 tweets (Twitter API limit for user timeline).
    """
    try:
        client = _get_client()

        user_resp = client.get_user(
            username=username,
            user_fields=["id"],
        )
        if not user_resp.data:
            return []

        user_id = user_resp.data.id
        all_tweets = []
        pagination_token = None

        while len(all_tweets) < max_tweets:
            batch_size = min(100, max_tweets - len(all_tweets))

            kwargs = {
                "id": user_id,
                "max_results": batch_size,
                "tweet_fields": ["text", "created_at"],
                "exclude": ["retweets"],
            }
            if pagination_token:
                kwargs["pagination_token"] = pagination_token

            response = client.get_users_tweets(**kwargs)

            if not response.data:
                break

            for tweet in response.data:
                all_tweets.append({
                    "text": tweet.text,
                    "created_at": str(tweet.created_at) if tweet.created_at else "",
                })

            # Check for more pages
            if response.meta and response.meta.get("next_token"):
                pagination_token = response.meta["next_token"]
                print(f"[twitter] fetched {len(all_tweets)} tweets so far from @{username}...")
            else:
                break

        print(f"[twitter] fetched {len(all_tweets)} total tweets from @{username}")
        return all_tweets

    except Exception as e:
        print(f"[twitter] get_user_full_history failed: {e}")
        return []


# ---------- Fetching user profile ----------

def get_user_profile(username: str) -> Optional[dict]:
    """Fetch a user's profile by username."""
    try:
        client = _get_client()
        response = client.get_user(
            username=username,
            user_fields=[
                "id", "username", "name", "description",
                "public_metrics", "profile_image_url", "created_at",
            ],
        )
        if not response.data:
            return None

        user = response.data
        return {
            "id": str(user.id),
            "username": user.username,
            "name": user.name,
            "bio": user.description or "",
            "followers": user.public_metrics.get("followers_count", 0) if user.public_metrics else 0,
            "following": user.public_metrics.get("following_count", 0) if user.public_metrics else 0,
            "profile_image_url": user.profile_image_url or "",
            "created_at": str(user.created_at) if user.created_at else "",
        }

    except Exception as e:
        print(f"[twitter] get_user_profile failed: {e}")
        return None


# ---------- Thread context ----------

def get_tweet(tweet_id: str) -> Optional[dict]:
    """Fetch a specific tweet by ID. Returns dict with text, username, etc."""
    try:
        client = _get_client()
        response = client.get_tweet(
            tweet_id,
            tweet_fields=["text", "author_id", "created_at"],
            expansions=["author_id"],
            user_fields=["username"],
        )
        if not response.data:
            return None

        username = "unknown"
        if response.includes and "users" in response.includes:
            username = response.includes["users"][0].username

        return {
            "tweet_id": str(response.data.id),
            "text": response.data.text,
            "username": username,
        }
    except Exception as e:
        print(f"[twitter] get_tweet failed: {e}")
        return None


def get_conversation_thread(conversation_id: str, current_tweet_id: str = None) -> List[dict]:
    """
    Fetch the full conversation thread above a tweet.
    Returns list of dicts with username and text, oldest first.
    Excludes the current tweet (the mention being replied to).
    """
    try:
        client = _get_client()
        response = client.search_recent_tweets(
            query=f"conversation_id:{conversation_id}",
            max_results=30,
            tweet_fields=["id", "text", "author_id", "created_at", "in_reply_to_user_id"],
            expansions=["author_id"],
            user_fields=["username"],
        )

        if not response.data:
            return []

        # Build author lookup
        authors = {}
        if response.includes and "users" in response.includes:
            for user in response.includes["users"]:
                authors[str(user.id)] = user.username

        thread = []
        for tweet in response.data:
            tid = str(tweet.id)
            if current_tweet_id and tid == current_tweet_id:
                continue
            thread.append({
                "username": authors.get(str(tweet.author_id), "unknown"),
                "text": tweet.text,
                "tweet_id": tid,
                "created_at": str(tweet.created_at) if tweet.created_at else "",
            })

        # Sort oldest first so the conversation reads top-to-bottom
        thread.sort(key=lambda t: t.get("created_at", ""))
        return thread

    except Exception as e:
        print(f"[twitter] get_conversation_thread failed: {e}")
        return []


# ---------- Tweet image downloads ----------

def download_tweet_images(tweet_id: str) -> List[Dict[str, str]]:
    """
    Fetch images attached to a specific tweet.
    Downloads and returns base64-encoded images ready for a vision model.
    Returns list of {"media_type": "image/jpeg", "data": "<base64>"}.
    """
    import base64
    import io
    import httpx
    from PIL import Image

    _MAX_RAW_FOR_API = 3_500_000

    try:
        client = _get_client()
        response = client.get_tweet(
            tweet_id,
            expansions=["attachments.media_keys"],
            media_fields=["url", "type", "preview_image_url"],
        )

        if not response.includes or "media" not in response.includes:
            return []

        images = []
        for media in response.includes["media"]:
            if media.type not in ("photo", "animated_gif"):
                continue
            # For animated GIFs, Twitter provides preview_image_url (static frame)
            # since models can't process animated GIFs as video
            url = media.url
            if media.type == "animated_gif":
                url = media.preview_image_url or url
            if not url:
                continue

            try:
                resp = httpx.get(url, timeout=30, follow_redirects=True)
                if resp.status_code != 200:
                    print(f"[twitter] image download failed: HTTP {resp.status_code}")
                    continue

                raw_bytes = resp.content
                media_type = "image/jpeg"

                content_type = resp.headers.get("content-type", "")
                if "png" in content_type:
                    media_type = "image/png"
                elif "webp" in content_type:
                    media_type = "image/webp"

                # Resize if too large for the model API
                if len(raw_bytes) > _MAX_RAW_FOR_API:
                    raw_bytes, media_type = _resize_twitter_image(raw_bytes)

                b64 = base64.b64encode(raw_bytes).decode("utf-8")
                images.append({"media_type": media_type, "data": b64})
                print(f"[twitter] downloaded image from tweet {tweet_id} ({len(raw_bytes)} bytes)")

            except Exception as dl_err:
                print(f"[twitter] image download error: {dl_err}")
                continue

        return images

    except Exception as e:
        print(f"[twitter] download_tweet_images failed: {e}")
        return []


def _resize_twitter_image(raw_bytes: bytes) -> tuple:
    """Resize a Twitter image to fit the model API limit. Returns (bytes, media_type)."""
    import io
    from PIL import Image

    _MAX_RAW_FOR_API = 3_500_000

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        for scale in [0.75, 0.5, 0.35, 0.25]:
            new_size = (int(img.width * scale), int(img.height * scale))
            resized = img.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=85)
            result = buf.getvalue()
            if len(result) <= _MAX_RAW_FOR_API:
                print(f"[twitter] resized image: {img.width}x{img.height} -> {new_size[0]}x{new_size[1]}")
                return (result, "image/jpeg")

        # Last resort
        resized = img.resize((800, int(800 * img.height / img.width)), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=70)
        return (buf.getvalue(), "image/jpeg")
    except Exception as e:
        print(f"[twitter] image resize failed: {e}")
        return (raw_bytes, "image/jpeg")
