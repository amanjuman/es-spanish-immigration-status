"""Enqueues periodic checks for monitored expedientes and handles their results
(state diff → Telegram notification, DB updates)."""

import asyncio
import logging
from datetime import datetime

from datetime import timedelta

from ..config import settings
from ..core.models import CheckRequest
from . import db, notify
from .limits import adaptive_interval
from .queue import Job, JobStatus, queue

log = logging.getLogger(__name__)

_scheduler_task: asyncio.Task | None = None
_last_purge: datetime | None = None


def start() -> None:
    global _scheduler_task
    queue.on_job_finished = _handle_finished_job
    _scheduler_task = asyncio.create_task(_loop(), name="monitor-scheduler")


async def stop() -> None:
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass


async def _loop() -> None:
    # Small initial delay so a crash-looping server doesn't hammer the site.
    await asyncio.sleep(15)
    while True:
        try:
            _enqueue_due_monitors()
            _purge_stale()
        except Exception:
            log.exception("Scheduler tick failed")
        await asyncio.sleep(60)


def base_interval() -> int:
    """Admin-configured base re-check interval (falls back to the env default);
    toggled live from the admin panel, so no restart is needed."""
    return db.get_int_setting("monitor_interval_seconds", settings.monitor_interval_seconds)


def capacity_floor() -> int:
    """Minimum interval any monitor is allowed right now, so total scheduled
    load stays within the shared queue's capacity."""
    return adaptive_interval(db.count_active_monitors(), 0, settings.check_spacing_seconds)


def monitor_interval(monitor: dict) -> int:
    """Effective interval for one monitor: its own override (or the global
    base), never faster than the capacity floor."""
    base = monitor["interval_seconds"] or base_interval()
    return max(base, capacity_floor())


def effective_interval() -> int:
    """Representative interval for the global base (admin panel / healthz),
    stretched to fit queue capacity when the instance is busy."""
    return adaptive_interval(
        db.count_active_monitors(),
        base_interval(),
        settings.check_spacing_seconds,
    )


def _enqueue_due_monitors() -> None:
    now = datetime.now()
    for monitor in db.list_monitors():
        if monitor["paused"] or queue.is_monitor_queued(monitor["id"]):
            continue
        last = monitor["last_checked_at"]
        due = last is None or (
            (now - datetime.fromisoformat(last)).total_seconds()
            >= monitor_interval(monitor)
        )
        if not due:
            continue
        request = CheckRequest(
            expediente_id=monitor["expediente_id"],
            fecha_presentacion=monitor["fecha_presentacion"],
            anio_nacimiento=monitor["anio_nacimiento"],
        )
        try:
            queue.submit(request, monitor_id=monitor["id"])
        except ValueError:
            pass


def _purge_stale() -> None:
    """Data hygiene, hourly. Resolved monitors are kept for a grace period
    then removed; in public mode, monitors nobody ever subscribed to are
    removed too (their creator lost the link or never activated alerts)."""
    global _last_purge
    now = datetime.now()
    if _last_purge and now - _last_purge < timedelta(hours=1):
        return
    _last_purge = now

    counts = db.subscriber_counts()
    for monitor in db.list_monitors():
        age = now - datetime.fromisoformat(monitor["created_at"])
        if monitor["resolved_at"]:
            resolved_age = now - datetime.fromisoformat(monitor["resolved_at"])
            if resolved_age > timedelta(days=settings.purge_resolved_after_days):
                log.info("Purging resolved monitor %s (resolved %s)",
                         monitor["id"], monitor["resolved_at"])
                db.delete_monitor(monitor["id"])
                continue
            # A final resolution won't change again — stop spending queue slots.
            if resolved_age > timedelta(days=2) and not monitor["paused"]:
                log.info("Auto-pausing resolved monitor %s", monitor["id"])
                db.set_monitor_paused(monitor["id"], True)
        elif (settings.public_mode
              and counts.get(monitor["id"], 0) == 0
              and age > timedelta(days=settings.purge_unclaimed_after_days)):
            log.info("Purging unclaimed monitor %s (no subscribers after %d days)",
                     monitor["id"], settings.purge_unclaimed_after_days)
            db.delete_monitor(monitor["id"])

    db.purge_history_older_than(90)


async def _handle_finished_job(job: Job) -> None:
    """Runs after every job. Persists history; for monitor jobs, diffs state
    and notifies."""
    expediente = job.request.expediente_id

    if job.status != JobStatus.DONE or job.result is None:
        db.add_history(expediente, ok=False, error=job.error or "unknown")
        if job.monitor_id is not None:
            db.update_monitor_result(job.monitor_id, state=None, error=job.error)
        return

    result = job.result
    db.add_history(
        expediente, ok=True, estado=result.estado, nie=result.nie,
        fecha_resolucion=result.fecha_resolucion,
    )

    if job.monitor_id is None:
        return

    monitor = db.get_monitor(job.monitor_id)
    if monitor is None:  # deleted while the check ran
        return

    new_state = result.state_key()
    old_state = monitor["last_state"]
    label = monitor["label"] or expediente

    message = None
    if old_state is None:
        message = (
            f"✅ <b>Monitoring started</b>\n"
            f"{label}\n"
            f"Expediente: <code>{expediente}</code>\n"
            f"Estado de Resolución: <code>{new_state['estado'] or '(vacío)'}</code>\n"
            f"You'll be notified when N.I.E., Estado or Fecha de Resolución change."
        )
    elif new_state != old_state:
        log.info("STATUS CHANGED for %s — was %s, now %s", expediente, old_state, new_state)
        message = (
            f"🚨 <b>Status change detected!</b>\n\n"
            f"{label}\n"
            f"Expediente: <code>{expediente}</code>\n"
            f"N.I.E: <code>{new_state['nie'] or '(vacío)'}</code>\n"
            f"Estado de Resolución: <code>{new_state['estado'] or '(vacío)'}</code>\n"
            f"Fecha de Resolución: <code>{new_state['fecha_resolucion'] or '(vacío)'}</code>\n\n"
            f"👉 Check: {settings.base_url}"
        )

    if message:
        for channel, address, cancel_code in _recipients(monitor):
            text = message
            if cancel_code:
                text += (f"\n\nStop these alerts: send /stop to this bot, or use "
                         f"code <code>{cancel_code}</code> on the web page.")
            await notify.send(channel, address, text)

    if new_state["fecha_resolucion"]:
        db.set_monitor_resolved(job.monitor_id)
    db.update_monitor_result(job.monitor_id, state=new_state, error=None)


def _recipients(monitor: dict) -> list[tuple[str, str, str | None]]:
    """(channel, address, cancel_code) for every subscriber. In private mode
    the legacy per-monitor chat id and the global fallback chat id are added
    too (without cancel codes); in public mode they are NOT — the operator
    must not receive other people's alerts."""
    recipients: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()
    for sub in db.subscriptions_for_monitor(monitor["id"]):
        if sub["chat_id"] not in seen:
            recipients.append((sub["channel"], sub["chat_id"], sub["cancel_code"]))
            seen.add(sub["chat_id"])
    if not settings.public_mode:
        for extra in (monitor["telegram_chat_id"], settings.telegram_chat_id):
            if extra and extra not in seen:
                recipients.append(("telegram", extra, None))
                seen.add(extra)
    return recipients
