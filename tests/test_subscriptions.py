from datetime import datetime, timedelta

import pytest

from app.web import bot, db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init(tmp_path / "test.sqlite3")
    yield


def make_monitor(expediente="E28202600000001", label="Test"):
    return db.add_monitor(label, expediente, "03/06/2026", "1990")


def make_update(chat_id, text, name="Ana"):
    return {"message": {"chat": {"id": chat_id}, "from": {"first_name": name},
                        "text": text}}


def test_link_code_claim_is_single_use():
    mid = make_monitor()
    link = db.create_link_code(mid)
    monitor, is_creator = db.claim_link_code(link["code"])
    assert monitor["id"] == mid and is_creator is False
    assert db.claim_link_code(link["code"]) == (None, False)


def test_creator_link_code_flag():
    mid = make_monitor()
    link = db.create_link_code(mid, is_creator=True)
    monitor, is_creator = db.claim_link_code(link["code"])
    assert monitor["id"] == mid and is_creator is True


def test_expired_link_code_rejected():
    mid = make_monitor()
    link = db.create_link_code(mid, ttl_hours=0)
    # expires_at == now → already expired for any later claim
    assert datetime.fromisoformat(link["expires_at"]) <= datetime.now() + timedelta(seconds=1)
    assert db.claim_link_code(link["code"]) == (None, False)


def test_subscription_dedup_and_cancel_by_code():
    mid = make_monitor()
    code1, created1 = db.add_subscription(mid, "111", "Ana")
    code2, created2 = db.add_subscription(mid, "111", "Ana")
    assert created1 and not created2
    assert code1 == code2
    assert db.cancel_subscription_by_code(code1.lower()) is True  # case-insensitive
    assert db.cancel_subscription_by_code(code1) is False
    assert db.subscriptions_for_monitor(mid) == []


def test_deleting_monitor_cascades_subscriptions():
    mid = make_monitor()
    db.add_subscription(mid, "111", "Ana")
    db.delete_monitor(mid)
    assert db.subscriptions_for_chat("111") == []


def test_bot_start_with_valid_code_subscribes():
    mid = make_monitor(label="Maria")
    link = db.create_link_code(mid)
    reply = bot.handle_update(make_update(111, f"/start {link['code']}"))
    assert "Alerts activated" in reply and "Maria" in reply
    subs = db.subscriptions_for_monitor(mid)
    assert len(subs) == 1 and subs[0]["chat_id"] == "111"
    assert subs[0]["cancel_code"] in reply


def test_bot_start_with_bad_code():
    reply = bot.handle_update(make_update(111, "/start nope"))
    assert "invalid" in reply.lower()


def test_bot_list_and_stop():
    mid1 = make_monitor("E28202600000001", "Uno")
    mid2 = make_monitor("E28202600000002", "Dos")
    db.add_subscription(mid1, "111", "Ana")
    db.add_subscription(mid2, "111", "Ana")

    listing = bot.handle_update(make_update(111, "/list"))
    assert "Uno" in listing and "Dos" in listing

    reply = bot.handle_update(make_update(111, "/stop 2"))
    assert "Dos" in reply
    assert len(db.subscriptions_for_chat("111")) == 1

    # single subscription: bare /stop works
    reply = bot.handle_update(make_update(111, "/stop"))
    assert "Uno" in reply
    assert db.subscriptions_for_chat("111") == []


def test_bot_stop_ambiguous_asks_for_number():
    mid1 = make_monitor("E28202600000001")
    mid2 = make_monitor("E28202600000002")
    db.add_subscription(mid1, "111")
    db.add_subscription(mid2, "111")
    reply = bot.handle_update(make_update(111, "/stop"))
    assert "/list" in reply
    assert len(db.subscriptions_for_chat("111")) == 2


def test_bot_stopall():
    mid = make_monitor()
    db.add_subscription(mid, "111")
    assert "1" in bot.handle_update(make_update(111, "/stopall"))
    assert db.subscriptions_for_chat("111") == []


def test_bot_command_with_botname_suffix():
    reply = bot.handle_update(make_update(111, "/list@SomeBot"))
    assert "no active subscriptions" in reply.lower()


def test_bot_ignores_non_message_updates():
    assert bot.handle_update({"edited_message": {}}) is None
