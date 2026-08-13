"""SQLite persistence for monitors and check history.

Plain sqlite3 on purpose: the app is single-process, low-traffic, and
self-hosted — an ORM or async driver would add dependencies for nothing.
"""

import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL DEFAULT '',
    expediente_id TEXT NOT NULL UNIQUE,
    fecha_presentacion TEXT NOT NULL,
    anio_nacimiento TEXT NOT NULL,
    telegram_chat_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_state TEXT,          -- JSON {nie, estado, fecha_resolucion}
    last_error TEXT,
    manage_token TEXT UNIQUE, -- capability: whoever holds it manages this monitor
    paused INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,         -- set when fecha_resolucion first appears
    interval_seconds INTEGER  -- per-monitor re-check interval; NULL = global base
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    chat_name TEXT NOT NULL DEFAULT '',
    cancel_code TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'telegram',  -- telegram | whatsapp (future)
    UNIQUE (monitor_id, chat_id)
);
CREATE TABLE IF NOT EXISTS link_codes (
    code TEXT PRIMARY KEY,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    is_creator INTEGER NOT NULL DEFAULT 0  -- creator's claim gets the manage link
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invite_codes (
    code TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    used_at TEXT,
    used_by TEXT              -- expediente that redeemed it (audit)
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expediente_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    estado TEXT NOT NULL DEFAULT '',
    nie TEXT NOT NULL DEFAULT '',
    fecha_resolucion TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
"""


def init(db_path: Path) -> None:
    global _conn
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.executescript(SCHEMA)
    _migrate()
    _conn.commit()


def _migrate() -> None:
    """Add columns that predate-this-version databases are missing."""
    def cols(table: str) -> set[str]:
        return {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}

    for table, column, ddl in [
        ("monitors", "manage_token", "ALTER TABLE monitors ADD COLUMN manage_token TEXT"),
        ("monitors", "paused", "ALTER TABLE monitors ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"),
        ("monitors", "resolved_at", "ALTER TABLE monitors ADD COLUMN resolved_at TEXT"),
        ("monitors", "interval_seconds", "ALTER TABLE monitors ADD COLUMN interval_seconds INTEGER"),
        ("subscriptions", "channel",
         "ALTER TABLE subscriptions ADD COLUMN channel TEXT NOT NULL DEFAULT 'telegram'"),
        ("link_codes", "is_creator",
         "ALTER TABLE link_codes ADD COLUMN is_creator INTEGER NOT NULL DEFAULT 0"),
    ]:
        if column not in cols(table):
            _conn.execute(ddl)
    # Backfill capability tokens for monitors created before they existed.
    for row in _conn.execute("SELECT id FROM monitors WHERE manage_token IS NULL"):
        _conn.execute("UPDATE monitors SET manage_token = ? WHERE id = ?",
                      (secrets.token_urlsafe(24), row["id"]))


def conn() -> sqlite3.Connection:
    assert _conn is not None, "db.init() not called"
    return _conn


def _monitor_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["last_state"] = json.loads(d["last_state"]) if d["last_state"] else None
    return d


def list_monitors() -> list[dict]:
    rows = conn().execute("SELECT * FROM monitors ORDER BY id").fetchall()
    return [_monitor_dict(r) for r in rows]


def get_monitor(monitor_id: int) -> dict | None:
    row = conn().execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
    return _monitor_dict(row) if row else None


def add_monitor(label: str, expediente_id: str, fecha_presentacion: str,
                anio_nacimiento: str, telegram_chat_id: str = "") -> int:
    cur = conn().execute(
        "INSERT INTO monitors (label, expediente_id, fecha_presentacion, anio_nacimiento,"
        " telegram_chat_id, created_at, manage_token) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (label, expediente_id, fecha_presentacion, anio_nacimiento,
         telegram_chat_id, datetime.now().isoformat(), secrets.token_urlsafe(24)),
    )
    conn().commit()
    return cur.lastrowid


def get_monitor_by_token(manage_token: str) -> dict | None:
    row = conn().execute("SELECT * FROM monitors WHERE manage_token = ?",
                         (manage_token,)).fetchone()
    return _monitor_dict(row) if row else None


def set_monitor_paused(monitor_id: int, paused: bool) -> None:
    conn().execute("UPDATE monitors SET paused = ? WHERE id = ?",
                   (int(paused), monitor_id))
    conn().commit()


def set_monitor_label(monitor_id: int, label: str) -> None:
    conn().execute("UPDATE monitors SET label = ? WHERE id = ?", (label, monitor_id))
    conn().commit()


def set_monitor_interval(monitor_id: int, seconds: int | None) -> None:
    """Per-monitor re-check interval; None clears the override (use global base)."""
    conn().execute("UPDATE monitors SET interval_seconds = ? WHERE id = ?",
                   (seconds, monitor_id))
    conn().commit()


def set_monitor_resolved(monitor_id: int) -> None:
    conn().execute(
        "UPDATE monitors SET resolved_at = COALESCE(resolved_at, ?) WHERE id = ?",
        (datetime.now().isoformat(), monitor_id),
    )
    conn().commit()


def count_active_monitors() -> int:
    return conn().execute("SELECT COUNT(*) FROM monitors WHERE paused = 0").fetchone()[0]


def count_monitors() -> int:
    return conn().execute("SELECT COUNT(*) FROM monitors").fetchone()[0]


def delete_monitor(monitor_id: int) -> None:
    conn().execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
    conn().commit()


def update_monitor_result(monitor_id: int, state: dict | None, error: str | None) -> None:
    conn().execute(
        "UPDATE monitors SET last_checked_at = ?, last_state = COALESCE(?, last_state),"
        " last_error = ? WHERE id = ?",
        (datetime.now().isoformat(),
         json.dumps(state) if state is not None else None,
         error, monitor_id),
    )
    conn().commit()


def add_history(expediente_id: str, ok: bool, estado: str = "", nie: str = "",
                fecha_resolucion: str = "", error: str = "") -> None:
    conn().execute(
        "INSERT INTO history (expediente_id, checked_at, ok, estado, nie,"
        " fecha_resolucion, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (expediente_id, datetime.now().isoformat(), int(ok), estado, nie,
         fecha_resolucion, error),
    )
    conn().commit()


def purge_history_older_than(days: int) -> int:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cur = conn().execute("DELETE FROM history WHERE checked_at < ?", (cutoff,))
    conn().commit()
    return cur.rowcount


def recent_history(expediente_id: str, limit: int = 20) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM history WHERE expediente_id = ? ORDER BY id DESC LIMIT ?",
        (expediente_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Telegram subscriptions ────────────────────────────────────────

# No ambiguous characters (0/O, 1/I/L) — cancel codes get typed by humans.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _gen_code(length: int) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def create_link_code(monitor_id: int, ttl_hours: int = 24, is_creator: bool = False) -> dict:
    """One-time code embedded in the t.me deep link. Claiming it (via the
    bot's /start) subscribes that chat to the monitor. The code generated at
    monitor creation is flagged is_creator so its claimer also receives the
    management link."""
    code = secrets.token_urlsafe(9)  # 12 chars, fits Telegram's start payload
    expires = (datetime.now() + timedelta(hours=ttl_hours)).isoformat()
    conn().execute("DELETE FROM link_codes WHERE expires_at < ?",
                   (datetime.now().isoformat(),))
    conn().execute(
        "INSERT INTO link_codes (code, monitor_id, expires_at, is_creator)"
        " VALUES (?, ?, ?, ?)",
        (code, monitor_id, expires, int(is_creator)),
    )
    conn().commit()
    return {"code": code, "expires_at": expires}


def claim_link_code(code: str) -> tuple[dict | None, bool]:
    """Single-use: returns (monitor, is_creator) and deletes the code, or
    (None, False) if the code is unknown/expired."""
    row = conn().execute("SELECT * FROM link_codes WHERE code = ?", (code,)).fetchone()
    if row is None:
        return None, False
    conn().execute("DELETE FROM link_codes WHERE code = ?", (code,))
    conn().commit()
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        return None, False
    return get_monitor(row["monitor_id"]), bool(row["is_creator"])


def add_subscription(monitor_id: int, chat_id: str, chat_name: str = "") -> tuple[str, bool]:
    """Returns (cancel_code, created). If the chat is already subscribed,
    returns the existing code with created=False."""
    existing = conn().execute(
        "SELECT cancel_code FROM subscriptions WHERE monitor_id = ? AND chat_id = ?",
        (monitor_id, chat_id),
    ).fetchone()
    if existing:
        return existing["cancel_code"], False
    while True:
        cancel_code = _gen_code(6)
        try:
            conn().execute(
                "INSERT INTO subscriptions (monitor_id, chat_id, chat_name, cancel_code,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (monitor_id, chat_id, chat_name, cancel_code, datetime.now().isoformat()),
            )
            break
        except sqlite3.IntegrityError:
            continue  # cancel_code collision — regenerate
    conn().commit()
    return cancel_code, True


def subscriptions_for_monitor(monitor_id: int) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM subscriptions WHERE monitor_id = ? ORDER BY id", (monitor_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def subscriptions_for_chat(chat_id: str) -> list[dict]:
    rows = conn().execute(
        "SELECT s.*, m.label, m.expediente_id, m.last_state FROM subscriptions s"
        " JOIN monitors m ON m.id = s.monitor_id WHERE s.chat_id = ? ORDER BY s.id",
        (chat_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["last_state"] = json.loads(d["last_state"]) if d["last_state"] else None
        result.append(d)
    return result


def delete_subscription(subscription_id: int) -> None:
    conn().execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
    conn().commit()


def delete_subscriptions_for_chat(chat_id: str) -> int:
    cur = conn().execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
    conn().commit()
    return cur.rowcount


def cancel_subscription_by_code(cancel_code: str) -> bool:
    cur = conn().execute("DELETE FROM subscriptions WHERE cancel_code = ?",
                         (cancel_code.strip().upper(),))
    conn().commit()
    return cur.rowcount > 0


# ── Runtime flags (toggleable from the admin panel) ───────────────

def get_flag(key: str, default: bool = False) -> bool:
    row = conn().execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else row["value"] == "1"


def set_flag(key: str, value: bool) -> None:
    _set_setting(key, "1" if value else "0")


def get_int_setting(key: str, default: int) -> int:
    row = conn().execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return default


def set_int_setting(key: str, value: int) -> None:
    _set_setting(key, str(int(value)))


def _set_setting(key: str, value: str) -> None:
    conn().execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn().commit()


# ── Invitation codes ──────────────────────────────────────────────

def create_invite_code(length: int = 6) -> str:
    while True:
        code = _gen_code(length)
        try:
            conn().execute(
                "INSERT INTO invite_codes (code, created_at) VALUES (?, ?)",
                (code, datetime.now().isoformat()),
            )
            break
        except sqlite3.IntegrityError:
            continue
    conn().commit()
    return code


def list_invite_codes() -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM invite_codes ORDER BY (used_at IS NOT NULL), created_at DESC")]


def invite_counts() -> dict:
    total = conn().execute("SELECT COUNT(*) FROM invite_codes").fetchone()[0]
    used = conn().execute(
        "SELECT COUNT(*) FROM invite_codes WHERE used_at IS NOT NULL").fetchone()[0]
    return {"total": total, "used": used, "unused": total - used}


def consume_invite_code(code: str, used_by: str = "") -> bool:
    """Atomically mark an unused code used. Returns False if the code is
    unknown or already redeemed."""
    cur = conn().execute(
        "UPDATE invite_codes SET used_at = ?, used_by = ? "
        "WHERE code = ? AND used_at IS NULL",
        (datetime.now().isoformat(), used_by, code.strip().upper()),
    )
    conn().commit()
    return cur.rowcount > 0


def refund_invite_code(code: str) -> None:
    """Return a code to the unused pool (if a redemption's monitor creation
    then failed)."""
    conn().execute(
        "UPDATE invite_codes SET used_at = NULL, used_by = NULL WHERE code = ?",
        (code.strip().upper(),),
    )
    conn().commit()


def delete_invite_code(code: str) -> None:
    conn().execute("DELETE FROM invite_codes WHERE code = ?", (code.strip().upper(),))
    conn().commit()


def subscriber_counts() -> dict[int, int]:
    rows = conn().execute(
        "SELECT monitor_id, COUNT(*) AS n FROM subscriptions GROUP BY monitor_id"
    ).fetchall()
    return {r["monitor_id"]: r["n"] for r in rows}
