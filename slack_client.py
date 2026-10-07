"""
All Slack operations. Draft review cards, chat replies, thread history,
channel context, image downloads, signature verification.
"""
import base64
import hashlib
import hmac
import io
import os
import time
from typing import Optional, List, Dict

import httpx
from PIL import Image
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_DRAFT_CHANNEL_ID = os.environ.get("SLACK_DRAFT_CHANNEL_ID", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "Agent")

_client = WebClient(token=SLACK_BOT_TOKEN)
_cached_bot_user_id: Optional[str] = None

# Max image size to download (10MB). Resized if needed before sending to the model.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Anthropic's limit is 5MB for base64-encoded images (most providers are similar).
# Base64 inflates by ~33%, so we target 3.5MB raw to stay safe.
_MAX_RAW_FOR_API = 3_500_000

# Image MIME types vision models accept
_SUPPORTED_IMAGE_TYPES = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}


# ---------- Image resize ----------

def _resize_image(raw_bytes: bytes, name: str) -> tuple:
    """
    Resize an image to fit within the model API's image limit.
    Returns (resized_bytes, media_type).
    Always outputs JPEG for compression efficiency.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))

        # Convert RGBA/P to RGB for JPEG
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Scale down progressively until under the limit
        quality = 85
        for scale in [0.75, 0.5, 0.35, 0.25]:
            new_size = (int(img.width * scale), int(img.height * scale))
            resized = img.resize(new_size, Image.LANCZOS)

            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=quality)
            result_bytes = buf.getvalue()

            if len(result_bytes) <= _MAX_RAW_FOR_API:
                print(
                    f"[images] resized {name}: {img.width}x{img.height} -> "
                    f"{new_size[0]}x{new_size[1]} ({len(result_bytes)} bytes)"
                )
                return (result_bytes, "image/jpeg")

        # Last resort: very small
        resized = img.resize((800, int(800 * img.height / img.width)), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=70)
        result_bytes = buf.getvalue()
        print(f"[images] resized {name} to 800px wide ({len(result_bytes)} bytes)")
        return (result_bytes, "image/jpeg")

    except Exception as e:
        print(f"[images] resize failed for {name}: {e}")
        return (raw_bytes, "image/png")


# ---------- Image downloads ----------

def download_images_from_event(event: dict) -> List[Dict[str, str]]:
    """
    Extract and download images from a Slack event's file attachments.

    Returns a list of dicts: [{"media_type": "image/png", "data": "<base64>"}]
    Empty list if no images or download fails.
    """
    files = event.get("files", [])
    if not files:
        return []

    images = []
    for f in files:
        mimetype = f.get("mimetype", "")
        if mimetype not in _SUPPORTED_IMAGE_TYPES:
            print(f"[images] skipping {f.get('name', '?')}: unsupported type: {mimetype}")
            continue

        size = f.get("size", 0)
        if size > _MAX_IMAGE_BYTES:
            print(f"[images] skipping {f.get('name', '?')}: {size} bytes exceeds limit")
            continue

        # Prefer url_private_download (direct download), fall back to url_private
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue

        try:
            resp = httpx.get(
                url,
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                timeout=30,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "unknown")
                raw_bytes = resp.content
                print(
                    f"[images] downloaded {f.get('name', '?')} "
                    f"({len(raw_bytes)} bytes, content-type: {content_type})"
                )

                # Use the actual content-type from the response if it's an image
                actual_media_type = _SUPPORTED_IMAGE_TYPES.get(mimetype, "image/png")
                if content_type.startswith("image/"):
                    actual_media_type = content_type.split(";")[0].strip()

                # Resize if too large for the model API
                if len(raw_bytes) > _MAX_RAW_FOR_API:
                    raw_bytes, actual_media_type = _resize_image(
                        raw_bytes, f.get("name", "image")
                    )

                b64 = base64.b64encode(raw_bytes).decode("utf-8")
                images.append({
                    "media_type": actual_media_type,
                    "data": b64,
                })
            else:
                print(f"[images] download failed for {f.get('name', '?')}: HTTP {resp.status_code}")
        except Exception as e:
            print(f"[images] download error for {f.get('name', '?')}: {e}")

    return images


# ---------- Bot identity ----------

def get_bot_user_id() -> Optional[str]:
    global _cached_bot_user_id
    if _cached_bot_user_id is not None:
        return _cached_bot_user_id
    try:
        response = _client.auth_test()
        _cached_bot_user_id = response["user_id"]
        return _cached_bot_user_id
    except SlackApiError as e:
        print(f"Slack auth_test failed: {e.response['error']}")
        return None


def get_user_info(user_id: str) -> Optional[dict]:
    """Get a Slack user's display name and real name."""
    try:
        response = _client.users_info(user=user_id)
        if response["ok"]:
            profile = response["user"].get("profile", {})
            return {
                "display_name": profile.get("display_name", ""),
                "real_name": profile.get("real_name", ""),
            }
        return None
    except SlackApiError as e:
        print(f"Slack users_info failed: {e.response['error']}")
        return None


# ---------- Draft review cards ----------

def post_draft_for_review(
    draft_text: str,
    draft_id: int,
    context_prompt: str,
    post_type: str,
    context_files: str = "",
    model_used: str = "",
) -> Optional[str]:
    """Post a draft to the review channel with approve/regen/reject buttons."""
    context_note = f" | Context: {context_files}" if context_files else ""
    # Show which model wrote it. Essential when you're A/B testing cheap models.
    model_label = model_used.split("/")[-1] if model_used else ""
    model_note = f" | Model: `{model_label}`" if model_label else ""

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Draft #{draft_id}*\n```{draft_text}```",
            },
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"Type: `{post_type}`{model_note} | Prompt: _{context_prompt[:120]}_{context_note}",
            }],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve & Post"},
                    "style": "primary",
                    "action_id": "approve",
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔄 Regenerate"},
                    "action_id": "regenerate",
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject"},
                    "style": "danger",
                    "action_id": "reject",
                    "value": str(draft_id),
                },
            ],
        },
    ]

    try:
        response = _client.chat_postMessage(
            channel=SLACK_DRAFT_CHANNEL_ID,
            blocks=blocks,
            text=f"{AGENT_NAME} draft: {draft_text}",
        )
        return response["ts"]
    except SlackApiError as e:
        print(f"Slack post failed: {e.response['error']}")
        return None


def post_status_update(parent_ts: str, status: str, text: str = ""):
    """Reply in-thread to a draft message with a status update."""
    try:
        _client.chat_postMessage(
            channel=SLACK_DRAFT_CHANNEL_ID,
            thread_ts=parent_ts,
            text=f"*{status}*{(': ' + text) if text else ''}",
        )
    except SlackApiError as e:
        print(f"Slack thread post failed: {e.response['error']}")


# ---------- Chat replies ----------

def post_chat_reply(
    channel: str,
    text: str,
    thread_ts: Optional[str] = None,
) -> Optional[str]:
    try:
        response = _client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
        )
        return response["ts"]
    except SlackApiError as e:
        print(f"Slack chat reply failed: {e.response['error']}")
        return None


# ---------- Thread & channel history ----------

def fetch_thread_history(
    channel: str,
    thread_ts: str,
    bot_user_id: Optional[str] = None,
    limit: int = 20,
) -> list:
    """Fetch thread history formatted as chat messages (user/assistant roles)."""
    if not bot_user_id:
        bot_user_id = get_bot_user_id()

    try:
        response = _client.conversations_replies(
            channel=channel, ts=thread_ts, limit=limit,
        )
        messages = response.get("messages", [])
    except SlackApiError as e:
        print(f"Slack conversations_replies failed: {e.response['error']}")
        return []

    history = []
    for msg in messages:
        text = msg.get("text", "").strip()
        if not text:
            continue
        user_id = msg.get("user") or msg.get("bot_id")
        if user_id == bot_user_id or msg.get("app_id"):
            history.append({"role": "assistant", "content": text})
        else:
            history.append({"role": "user", "content": text})

    # Drop the last message since caller adds the current one
    if history and history[-1]["role"] == "user":
        history = history[:-1]

    return history


def fetch_recent_channel_messages(
    channel: str,
    limit: int = 10,
    exclude_ts: Optional[str] = None,
) -> str:
    """Fetch recent channel messages as raw text for scene-setting context."""
    try:
        response = _client.conversations_history(
            channel=channel, limit=limit + 5,
        )
        messages = response.get("messages", [])
    except SlackApiError as e:
        print(f"Slack conversations_history failed: {e.response['error']}")
        return ""

    messages = list(reversed(messages))
    lines = []
    for msg in messages:
        if msg.get("subtype") and msg.get("subtype") not in {"thread_broadcast"}:
            continue
        if exclude_ts and msg.get("ts") == exclude_ts:
            continue
        text = msg.get("text", "").strip()
        if not text:
            continue
        lines.append(text)

    return "\n".join(lines[-limit:])


def has_bot_posted_in_thread(
    channel: str,
    thread_ts: str,
    bot_user_id: Optional[str] = None,
) -> bool:
    if not bot_user_id:
        bot_user_id = get_bot_user_id()
    if not bot_user_id:
        return False

    try:
        response = _client.conversations_replies(
            channel=channel, ts=thread_ts, limit=50,
        )
        messages = response.get("messages", [])
    except SlackApiError as e:
        print(f"Slack conversations_replies (check) failed: {e.response['error']}")
        return False

    for msg in messages:
        user_id = msg.get("user") or msg.get("bot_id")
        if user_id == bot_user_id or msg.get("app_id"):
            return True
    return False


# ---------- Signature verification ----------

def verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    if not SLACK_SIGNING_SECRET:
        return False
    if abs(time.time() - int(timestamp)) > 300:
        return False
    basestring = f"v0:{timestamp}:".encode() + body
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), basestring, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)
