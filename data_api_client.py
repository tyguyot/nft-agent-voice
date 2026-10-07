"""
Optional live data. Point DATA_API_BASE at any JSON API your project exposes
(collection stats, game leaderboard, event schedule) and map trigger phrases to
endpoints below. When a Slack message contains a phrase, the endpoint is fetched,
compressed by the PREPROCESS model, and handed to the character as one short note.

Leave LIVE_DATA_TRIGGERS empty to disable.
"""
import os
from typing import Optional, Tuple

import httpx

DATA_API_BASE = os.environ.get("DATA_API_BASE", "").rstrip("/")
_http = httpx.Client(timeout=10)

# phrase -> (label, path, query params)
# Keep phrases specific. "floor" alone will fire on "dance floor".
LIVE_DATA_TRIGGERS = {
    # "floor price":   ("stats", "/stats", {}),
    # "recent sales":  ("sales", "/sales", {"limit": 5}),
    # "leaderboard":   ("leaderboard", "/leaderboard", {"limit": 5}),
}


def match_trigger(text: str) -> Optional[Tuple[str, str, dict]]:
    if not DATA_API_BASE or not LIVE_DATA_TRIGGERS:
        return None
    lower = (text or "").lower()
    for phrase, target in LIVE_DATA_TRIGGERS.items():
        if phrase in lower:
            return target
    return None


def fetch(path: str, params: dict = None):
    try:
        resp = _http.get(f"{DATA_API_BASE}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[data_api] {path} failed: {e}")
        return None
