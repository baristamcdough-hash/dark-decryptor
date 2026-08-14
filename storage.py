"""PoriotCloud Vault storage — SQLite metadata + JSON files on disk.

By design this is ephemeral: every vault has a TTL (default 6h) after which
the file is deleted. No volume needed to run — on Railway, data simply
resets on redeploy unless you attach a volume at /app/data.
"""
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("VAULT_DATA_DIR", str(BASE_DIR / "data")))
VAULTS_DIR = DATA_DIR / "vaults"
DB_PATH = DATA_DIR / "vault.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
VAULTS_DIR.mkdir(parents=True, exist_ok=True)

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SHORT_ID_LEN = 8

_lock = threading.Lock()
_conn = None


def ttl_seconds() -> float:
    return float(os.environ.get("VAULT_TTL_HOURS", "6")) * 3600


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS vaults (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL DEFAULT 'Config',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            views      INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()


def _new_short_id() -> str:
    while True:
        vid = "".join(secrets.choice(ALPHABET) for _ in range(SHORT_ID_LEN))
        if get_row(vid) is None:
            return vid


# --------------------------------------------------------------------------
# Vaults
# --------------------------------------------------------------------------

def create_vault(config_text: str, name: str = "Config") -> dict:
    """Store a vault. Returns {id, name, url_path, expires_at}."""
    now = time.time()
    vid = _new_short_id()
    expires = now + ttl_seconds()

    (VAULTS_DIR / f"{vid}.json").write_text(config_text, encoding="utf-8")
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO vaults (id, name, created_at, expires_at, views) VALUES (?,?,?,?,0)",
            (vid, name[:120], now, expires),
        )
        conn.commit()
    return {
        "id": vid,
        "name": name[:120],
        "url_path": f"/v/{vid}",
        "expires_at": expires,
    }


def get_row(vid: str):
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT * FROM vaults WHERE id = ?", (vid,)).fetchone()
        return dict(row) if row else None


def get_config_text(vid: str):
    path = VAULTS_DIR / f"{vid}.json"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def delete_vault(vid: str) -> bool:
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM vaults WHERE id = ?", (vid,))
        conn.commit()
    (VAULTS_DIR / f"{vid}.json").unlink(missing_ok=True)
    return cur.rowcount > 0


def incr_views(vid: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("UPDATE vaults SET views = views + 1 WHERE id = ?", (vid,))
        conn.commit()


def is_expired(row: dict, now: float | None = None) -> bool:
    return (now if now is not None else time.time()) > row["expires_at"]


def cleanup_expired(now: float | None = None) -> int:
    """Delete every expired vault. Returns how many were removed."""
    now = now if now is not None else time.time()
    with _lock:
        conn = _get_conn()
        rows = conn.execute("SELECT id FROM vaults WHERE expires_at <= ?", (now,)).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.execute("DELETE FROM vaults WHERE expires_at <= ?", (now,))
            conn.commit()
    for vid in ids:
        (VAULTS_DIR / f"{vid}.json").unlink(missing_ok=True)
    return len(ids)


def list_vaults(limit: int = 200) -> list[dict]:
    now = time.time()
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM vaults ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["expired"] = now > d["expires_at"]
        d["remaining_h"] = max(0.0, (d["expires_at"] - now) / 3600)
        d["created_at_iso"] = _iso(d["created_at"])
        d["expires_at_iso"] = _iso(d["expires_at"])
        out.append(d)
    return out


def stats() -> dict:
    now = time.time()
    with _lock:
        conn = _get_conn()
        total = conn.execute("SELECT COUNT(*) c FROM vaults").fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) c FROM vaults WHERE expires_at > ?", (now,)
        ).fetchone()["c"]
        views = conn.execute(
            "SELECT COALESCE(SUM(views),0) s FROM vaults"
        ).fetchone()["s"]
    return {"total": total, "active": active, "views": views}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# --------------------------------------------------------------------------
# Settings (Adsterra ad manager)
# --------------------------------------------------------------------------

DEFAULTS = {
    "ad_enabled": "1",
    "ad_position": "all",   # top | bottom | all | off
    "ad_code": "",          # Adsterra zone snippet, pasted by admin
}


def get_setting(key: str) -> str:
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS.get(key, "")


def get_settings() -> dict:
    return {k: get_setting(k) for k in DEFAULTS}


def set_setting(key: str, value: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def save_settings(settings: dict) -> None:
    for k, v in settings.items():
        if k in DEFAULTS:
            set_setting(k, v)
