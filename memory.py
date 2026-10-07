"""
Database layer. Raw SQLite, no ORM.
Tables: drafts, profiles, observations, replied_tweets, kv_store, collection_metadata.

Profiles are the personality driver: short, hand-written descriptions of
community members that give the character something specific to riff on.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "/data/agent.db")
PROFILES_DIR = Path(os.environ.get("PROFILES_DIR", "./profiles"))


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drafts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                context_prompt  TEXT NOT NULL,
                post_type       TEXT NOT NULL DEFAULT 'general',
                draft_text      TEXT NOT NULL,
                model_used      TEXT,
                context_files   TEXT,
                slack_ts        TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                posted_at       TEXT,
                tweet_id        TEXT,
                feedback        TEXT,
                feedback_author TEXT,
                reply_to_id     TEXT,
                quote_tweet_id  TEXT
            )
        """)
        # Migrate existing tables: add new columns if they don't exist
        for col, coltype in [("reply_to_id", "TEXT"), ("quote_tweet_id", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE drafts ADD COLUMN {col} {coltype}")
            except Exception:
                pass  # Column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                twitter_handle  TEXT NOT NULL UNIQUE,
                discord_handle  TEXT,
                wallet          TEXT,
                description     TEXT NOT NULL,
                tokens      TEXT,
                badges          TEXT,
                tier            TEXT,
                notes           TEXT,
                updated_at      TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL,
                author      TEXT,
                content     TEXT NOT NULL,
                channel     TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_drafts_slack_ts ON drafts(slack_ts)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_profiles_twitter ON profiles(twitter_handle)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS replied_tweets (
                tweet_id    TEXT PRIMARY KEY,
                replied_at  TEXT NOT NULL
            )
        """)
        # Store the last seen mention ID so it survives restarts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL
            )
        """)
        # Optional NFT collection metadata: token ID -> traits (any schema)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS collection_metadata (
                token_id    INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                traits_json TEXT NOT NULL DEFAULT '{}',
                image_url   TEXT
            )
        """)


# ---------- Collection metadata ----------

def get_token_metadata(token_id: int) -> Optional[dict]:
    """Look up a token's traits by ID. Returns {token_id, name, traits: {...}, image_url}."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM collection_metadata WHERE token_id = ?", (token_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["traits"] = json.loads(d.pop("traits_json") or "{}")
        return d


def search_tokens_by_trait(trait_type: str, trait_value: str, limit: int = 10) -> list:
    """Find tokens whose trait_type contains trait_value (case-insensitive)."""
    q = f"%{trait_value.lower()}%"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM collection_metadata WHERE LOWER(traits_json) LIKE ?",
            (q,),
        ).fetchall()
    results = []
    for r in rows:
        traits = json.loads(r["traits_json"] or "{}")
        value = str(traits.get(trait_type.lower(), "")).lower()
        if trait_value.lower() in value:
            results.append({"token_id": r["token_id"], "name": r["name"],
                            "traits": traits, "image_url": r["image_url"]})
            if len(results) >= limit:
                break
    return results


def get_metadata_count() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM collection_metadata").fetchone()
        return row["cnt"] if row else 0


def upsert_token_metadata(token_id: int, name: str, traits: dict, image_url: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO collection_metadata
               (token_id, name, traits_json, image_url) VALUES (?, ?, ?, ?)""",
            (token_id, name, json.dumps(traits), image_url),
        )


# ---------- Replied tweets ----------

def has_replied_to(tweet_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM replied_tweets WHERE tweet_id = ?", (tweet_id,)
        ).fetchone()
        return row is not None


def mark_replied(tweet_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO replied_tweets (tweet_id, replied_at) VALUES (?, ?)",
            (tweet_id, now),
        )


# ---------- KV store (for last_mention_id etc) ----------

def kv_get(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM kv_store WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
            (key, value),
        )


# ---------- Drafts ----------

def save_draft(
    context_prompt: str,
    post_type: str,
    draft_text: str,
    model_used: str = "",
    context_files: str = "",
    reply_to_id: str = "",
    quote_tweet_id: str = "",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO drafts
               (created_at, context_prompt, post_type, draft_text, model_used,
                context_files, status, reply_to_id, quote_tweet_id)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (now, context_prompt, post_type, draft_text, model_used,
             context_files, reply_to_id, quote_tweet_id),
        )
        return cursor.lastrowid


def get_draft(draft_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return dict(row) if row else None


def update_draft(draft_id: int, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [draft_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE drafts SET {set_clause} WHERE id = ?", values)


def get_recent_drafts(days: int = 7, limit: int = 50) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM drafts WHERE created_at >= ? ORDER BY created_at ASC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- Profiles ----------

def get_profile(twitter_handle: str) -> Optional[dict]:
    """Look up a community member by Twitter handle. Case-insensitive."""
    handle = twitter_handle.lower().lstrip("@")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE LOWER(twitter_handle) = ?",
            (handle,),
        ).fetchone()
        return dict(row) if row else None


def search_profiles(query: str, include_description: bool = False) -> list:
    """Search profiles by handle. Description search is opt-in: matching plain
    words against descriptions pulls in the wrong people ("context bleed")."""
    q = f"%{query.lower()}%"
    sql = """SELECT * FROM profiles
             WHERE LOWER(twitter_handle) LIKE ?
                OR LOWER(COALESCE(discord_handle, '')) LIKE ?"""
    params = [q, q]
    if include_description:
        sql += " OR LOWER(description) LIKE ?"
        params.append(q)
    sql += " LIMIT 10"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_profile_by_nickname(name: str) -> Optional[dict]:
    """
    Look up a profile by nickname/name with priority:
    1. Exact discord_handle match (case-insensitive)
    2. Exact twitter_handle match (case-insensitive)
    3. "Also goes by" or "Also known as" in description
    4. Fuzzy match on handles only, preferring shortest match (closest to query)
    """
    name_lower = name.lower().strip().lstrip("@")
    with get_conn() as conn:
        # 1. Exact discord_handle
        row = conn.execute(
            "SELECT * FROM profiles WHERE LOWER(discord_handle) = ?",
            (name_lower,),
        ).fetchone()
        if row:
            return dict(row)

        # 2. Exact twitter_handle
        row = conn.execute(
            "SELECT * FROM profiles WHERE LOWER(twitter_handle) = ?",
            (name_lower,),
        ).fetchone()
        if row:
            return dict(row)

        # 3. Check "Also goes by [name]" in description
        rows = conn.execute(
            "SELECT * FROM profiles WHERE LOWER(description) LIKE ?",
            (f"%also goes by {name_lower}%",),
        ).fetchall()
        if rows:
            return dict(rows[0])

        # 4. Fuzzy on handles only, prefer shortest match
        q = f"%{name_lower}%"
        rows = conn.execute(
            """SELECT * FROM profiles
               WHERE LOWER(twitter_handle) LIKE ?
                  OR LOWER(COALESCE(discord_handle, '')) LIKE ?""",
            (q, q),
        ).fetchall()
        if rows:
            # Sort by handle length: shorter = closer match
            results = [dict(r) for r in rows]
            results.sort(key=lambda r: min(len(r["twitter_handle"] or ""), len(r["discord_handle"] or "") or 999))
            return results[0]

        return None


def upsert_profile(
    twitter_handle: str,
    description: str,
    discord_handle: str = "",
    wallet: str = "",
    tokens: str = "",
    badges: str = "",
    tier: str = "",
    notes: str = "",
) -> int:
    """Insert or update a community member profile."""
    handle = twitter_handle.lower().lstrip("@")
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM profiles WHERE LOWER(twitter_handle) = ?", (handle,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE profiles SET
                   description=?, discord_handle=?, wallet=?, tokens=?,
                   badges=?, tier=?, notes=?, updated_at=?
                   WHERE id=?""",
                (description, discord_handle, wallet, tokens,
                 badges, tier, notes, now, existing["id"]),
            )
            return existing["id"]
        else:
            cursor = conn.execute(
                """INSERT INTO profiles
                   (twitter_handle, discord_handle, wallet, description,
                    tokens, badges, tier, notes, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (handle, discord_handle, wallet, description,
                 tokens, badges, tier, notes, now),
            )
            return cursor.lastrowid


def get_all_profiles() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM profiles ORDER BY twitter_handle"
        ).fetchall()
        return [dict(r) for r in rows]


def load_profiles_from_json(path: str = None) -> int:
    """
    Bulk load profiles from a JSON file. Used for initial seeding.
    Expected format: list of objects with twitter_handle, description, etc.
    Returns number of profiles loaded.
    """
    if path is None:
        path = PROFILES_DIR / "community.json"
    path = Path(path)
    if not path.exists():
        return 0

    with open(path, "r", encoding="utf-8") as f:
        profiles = json.load(f)

    count = 0
    for p in profiles:
        if "twitter_handle" in p and "description" in p:
            upsert_profile(**{k: v for k, v in p.items() if k in (
                "twitter_handle", "description", "discord_handle",
                "wallet", "tokens", "badges", "tier", "notes",
            )})
            count += 1
    return count


# ---------- Observations ----------

def save_observation(
    source: str,
    content: str,
    author: str = "",
    channel: str = "",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO observations (source, author, content, channel, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (source, author, content, channel, now),
        )
        return cursor.lastrowid


def get_recent_observations(
    source: str = "",
    author: str = "",
    hours: int = 24,
    limit: int = 20,
) -> list:
    """Get recent observations, optionally filtered by source and author."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        if source and author:
            rows = conn.execute(
                """SELECT * FROM observations
                   WHERE source = ? AND author = ? AND created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (source, author, cutoff, limit),
            ).fetchall()
        elif source:
            rows = conn.execute(
                """SELECT * FROM observations
                   WHERE source = ? AND created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (source, cutoff, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM observations
                   WHERE created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def search_observations(query: str, hours: int = 48, limit: int = 5) -> list:
    """Search observations by keyword."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q = f"%{query.lower()}%"
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM observations
               WHERE LOWER(content) LIKE ? AND created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (q, cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]
