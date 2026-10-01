import asyncio
import os
import time

import pytest

from app.core.browser import StatusChecker
from app.core.models import InvalidInputError
from app.web import db, scheduler
from app.web.queue import Job, JobStatus
from app.core.models import CheckRequest


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init(tmp_path / "test.sqlite3")
    yield


# ── Detection of the portal's validation message ──────────────────

REJECTED_PAGE = """<html><body><form>
  <label>ID de expediente/solicitud</label><input value="Z1234567X">
  <div class="error">El número de expediente introducido no es válido</div>
</form></body></html>"""

# Same message present in the DOM but hidden — must NOT count as a rejection.
HIDDEN_TEMPLATE_PAGE = """<html><body><form>
  <input value="E28202600000001">
  <div style="display:none">El número de expediente introducido no es válido</div>
</form></body></html>"""

NORMAL_FORM_PAGE = """<html><body><form>
  <p>* Por favor, valida el Captcha para poder continuar</p>
</form></body></html>"""


def _detect(html: str) -> str | None:
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = await browser.new_page()
                await page.set_content(html)
                return await StatusChecker._visible_validation_error(page)
            finally:
                await browser.close()

    return asyncio.run(run())


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


needs_chromium = pytest.mark.skipif(not _chromium_available(),
                                    reason="Playwright Chromium not installed")


@needs_chromium
def test_visible_rejection_detected():
    msg = _detect(REJECTED_PAGE)
    assert msg == "El número de expediente introducido no es válido"


@needs_chromium
def test_hidden_error_template_not_detected():
    assert _detect(HIDDEN_TEMPLATE_PAGE) is None


@needs_chromium
def test_normal_form_not_detected():
    # "valida el Captcha" must not be mistaken for "no es válido"
    assert _detect(NORMAL_FORM_PAGE) is None


# ── Error typing ───────────────────────────────────────────────────

def test_invalid_input_error_code_and_message():
    e = InvalidInputError("El número de expediente introducido no es válido")
    assert e.code == "invalid_input"
    assert "no es válido" in str(e)
    assert "N.I.E" in str(e)


# ── Scheduler: pause + notify on invalid input ────────────────────

def test_invalid_input_pauses_monitor_and_notifies(monkeypatch):
    mid = db.add_monitor("Test", "Z1234567X", "01/05/2026", "1990")
    db.add_subscription(mid, "111", "Ana")

    sent = []

    async def fake_send(channel, address, message):
        sent.append((channel, address, message))
        return True

    monkeypatch.setattr(scheduler.notify, "send", fake_send)

    job = Job(request=CheckRequest("Z1234567X", "01/05/2026", "1990"), monitor_id=mid)
    job.status = JobStatus.ERROR
    job.error = str(InvalidInputError("El número de expediente introducido no es válido"))
    job.error_code = "invalid_input"

    asyncio.run(scheduler._handle_finished_job(job))

    assert db.get_monitor(mid)["paused"] == 1
    assert len(sent) == 1
    assert sent[0][1] == "111"
    assert "Monitoring paused" in sent[0][2]


def test_other_failures_do_not_pause(monkeypatch):
    mid = db.add_monitor("Test", "E28202600000001", "03/06/2026", "1990")

    async def fake_send(*a):
        return True

    monkeypatch.setattr(scheduler.notify, "send", fake_send)

    job = Job(request=CheckRequest("E28202600000001", "03/06/2026", "1990"), monitor_id=mid)
    job.status = JobStatus.ERROR
    job.error = "Could not solve the captcha after 5 attempts."
    job.error_code = "captcha"

    asyncio.run(scheduler._handle_finished_job(job))
    assert db.get_monitor(mid)["paused"] == 0


# ── Debug screenshot pruning ──────────────────────────────────────

def test_prune_debug_screenshots(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler.settings, "data_dir", tmp_path)
    debug = scheduler.settings.debug_dir
    debug.mkdir(parents=True, exist_ok=True)
    old = debug / "old_job_result.png"
    new = debug / "new_job_result.png"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old, (ten_days_ago, ten_days_ago))

    assert scheduler._prune_debug_screenshots(days=7) == 1
    assert not old.exists() and new.exists()
