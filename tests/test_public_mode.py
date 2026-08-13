import pytest

from app.core.models import CheckResult
from app.web import db
from app.web.limits import RateLimiter, ResultCache, adaptive_interval


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init(tmp_path / "test.sqlite3")
    yield


def test_manage_token_generated_and_looked_up():
    mid = db.add_monitor("Test", "E28202600000001", "03/06/2026", "1990")
    monitor = db.get_monitor(mid)
    token = monitor["manage_token"]
    assert token and len(token) >= 24
    assert db.get_monitor_by_token(token)["id"] == mid
    assert db.get_monitor_by_token("wrong") is None


def test_pause_resume_and_active_count():
    mid = db.add_monitor("Test", "E28202600000001", "03/06/2026", "1990")
    assert db.count_active_monitors() == 1
    db.set_monitor_paused(mid, True)
    assert db.count_active_monitors() == 0
    assert db.get_monitor(mid)["paused"] == 1
    db.set_monitor_paused(mid, False)
    assert db.count_active_monitors() == 1


def test_resolved_at_set_once():
    mid = db.add_monitor("Test", "E28202600000001", "03/06/2026", "1990")
    db.set_monitor_resolved(mid)
    first = db.get_monitor(mid)["resolved_at"]
    db.set_monitor_resolved(mid)
    assert db.get_monitor(mid)["resolved_at"] == first


def test_client_ip_prefers_cf_connecting_ip(monkeypatch):
    from types import SimpleNamespace

    from app.web import limits

    def req(headers, conn="127.0.0.1"):
        return SimpleNamespace(headers=headers, client=SimpleNamespace(host=conn))

    monkeypatch.setattr(limits.settings, "trust_proxy", True)
    # CF header wins over a spoofed X-Forwarded-For
    assert limits.client_ip(req({
        "cf-connecting-ip": "8.8.8.8",
        "x-forwarded-for": "192.168.0.5",
    })) == "8.8.8.8"
    # falls back to XFF when no CF header
    assert limits.client_ip(req({"x-forwarded-for": "9.9.9.9, 10.0.0.1"})) == "9.9.9.9"
    # without trust_proxy, headers are ignored
    monkeypatch.setattr(limits.settings, "trust_proxy", False)
    assert limits.client_ip(req({"cf-connecting-ip": "8.8.8.8"})) == "127.0.0.1"


def test_turnstile_exempt_cidrs():
    from app.web.limits import turnstile_exempt
    assert turnstile_exempt("192.168.1.50")      # LAN
    assert turnstile_exempt("100.100.5.9")       # Tailscale CGNAT
    assert turnstile_exempt("127.0.0.1")         # loopback
    assert not turnstile_exempt("8.8.8.8")       # public
    assert not turnstile_exempt("not-an-ip")


def test_adaptive_interval():
    # few monitors: base interval wins
    assert adaptive_interval(5, 3600, 60) == 3600
    # many monitors: stretches to 2*N*spacing
    assert adaptive_interval(40, 3600, 60) == 4800
    assert adaptive_interval(100, 3600, 60) == 12000


def test_rate_limiter_window():
    rl = RateLimiter()
    for _ in range(3):
        assert rl.allow("b", "ip1", 3, 60)
    assert not rl.allow("b", "ip1", 3, 60)
    assert rl.allow("b", "ip2", 3, 60)  # other client unaffected


def test_result_cache_roundtrip_and_ttl():
    cache = ResultCache(ttl_seconds=0)  # everything expires immediately
    result = CheckResult(fields={"Estado de Resolución": "EN TRAMITE"})
    cache.put("E1", "01/01/2026", "1990", result)
    assert cache.get("E1", "01/01/2026", "1990") is None

    cache = ResultCache(ttl_seconds=60)
    cache.put("E1", "01/01/2026", "1990", result)
    assert cache.get("E1", "01/01/2026", "1990").estado == "EN TRAMITE"
    assert cache.get("E2", "01/01/2026", "1990") is None


def test_runtime_flag_default_and_toggle():
    assert db.get_flag("invite_only", False) is False
    assert db.get_flag("invite_only", True) is True   # default honoured
    db.set_flag("invite_only", True)
    assert db.get_flag("invite_only", False) is True
    db.set_flag("invite_only", False)
    assert db.get_flag("invite_only", True) is False


def test_int_setting_default_and_override():
    assert db.get_int_setting("monitor_interval_seconds", 3600) == 3600
    db.set_int_setting("monitor_interval_seconds", 21600)
    assert db.get_int_setting("monitor_interval_seconds", 3600) == 21600


def test_adaptive_interval_respects_runtime_base():
    # base wins when instance is idle; stretch applies when busy
    assert adaptive_interval(2, 21600, 60) == 21600
    assert adaptive_interval(300, 3600, 60) == 36000


def test_per_monitor_interval_override():
    from app.web import scheduler
    mid = db.add_monitor("Test", "E28202600000009", "03/06/2026", "1990")
    # default: follows global base (3600 when idle)
    m = db.get_monitor(mid)
    assert m["interval_seconds"] is None
    assert scheduler.monitor_interval(m) == 3600
    # override to 6h
    db.set_monitor_interval(mid, 21600)
    m = db.get_monitor(mid)
    assert m["interval_seconds"] == 21600
    assert scheduler.monitor_interval(m) == 21600
    # capacity floor still wins if it's higher than the override
    m["interval_seconds"] = 3600
    assert scheduler.monitor_interval({**m, "interval_seconds": 3600}) >= 3600
    # clear override
    db.set_monitor_interval(mid, None)
    assert db.get_monitor(mid)["interval_seconds"] is None


def test_invite_code_one_time_use():
    code = db.create_invite_code()
    assert len(code) == 6
    assert db.invite_counts() == {"total": 1, "used": 0, "unused": 1}
    # case-insensitive redemption
    assert db.consume_invite_code(code.lower(), used_by="E28X") is True
    # second use fails
    assert db.consume_invite_code(code) is False
    assert db.invite_counts() == {"total": 1, "used": 1, "unused": 0}
    # audit trail
    row = [c for c in db.list_invite_codes() if c["code"] == code][0]
    assert row["used_by"] == "E28X"


def test_invite_refund_restores_code():
    code = db.create_invite_code()
    db.consume_invite_code(code, used_by="E28X")
    db.refund_invite_code(code)
    assert db.invite_counts()["unused"] == 1
    assert db.consume_invite_code(code) is True


def test_unknown_invite_code_rejected():
    assert db.consume_invite_code("ZZZZZZ") is False


def test_migration_backfills_manage_token(tmp_path):
    """A DB created by the pre-public schema gets tokens on init()."""
    import sqlite3
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL DEFAULT '',
            expediente_id TEXT NOT NULL UNIQUE,
            fecha_presentacion TEXT NOT NULL,
            anio_nacimiento TEXT NOT NULL,
            telegram_chat_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_checked_at TEXT, last_state TEXT, last_error TEXT
        );
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL, chat_name TEXT NOT NULL DEFAULT '',
            cancel_code TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        );
        CREATE TABLE link_codes (
            code TEXT PRIMARY KEY, monitor_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        );
        INSERT INTO monitors (label, expediente_id, fecha_presentacion,
            anio_nacimiento, created_at)
        VALUES ('Old', 'E28OLD', '01/01/2026', '1990', '2026-07-01T00:00:00');
    """)
    conn.commit()
    conn.close()

    db.init(path)
    monitor = db.list_monitors()[0]
    assert monitor["manage_token"]
    assert monitor["paused"] == 0
