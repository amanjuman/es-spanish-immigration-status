import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..config import settings
from ..core.models import CheckRequest
from . import bot, db, scheduler
from .limits import client_ip, rate_limiter, result_cache, verify_turnstile
from .queue import queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(settings.data_dir / "notifier.log"),
    ],
)
# httpx logs full request URLs at INFO — for Telegram calls that includes the
# bot token, which must not end up in log files.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init(settings.db_path)
    queue.start()
    scheduler.start()
    bot.start()
    log.info("Started — captcha provider: %s, spacing: %ss, monitor interval: %ss",
             settings.captcha_provider, settings.check_spacing_seconds,
             settings.monitor_interval_seconds)
    yield
    await bot.stop()
    await scheduler.stop()
    await queue.stop()


app = FastAPI(title="Spanish Immigration Status Notifier", lifespan=lifespan)


def invite_only_enabled() -> bool:
    return db.get_flag("invite_only", settings.invite_only)


def _page_ctx() -> dict:
    return {
        "public_mode": settings.public_mode,
        "turnstile_site_key": settings.turnstile_site_key,
        "invite_only": invite_only_enabled(),
    }


def _admin_token(request: Request) -> str:
    return (request.headers.get("x-admin-token")
            or request.query_params.get("admin_token") or "")


def _is_admin(request: Request) -> bool:
    return bool(settings.admin_token) and _admin_token(request) == settings.admin_token


def _require_admin(request: Request) -> None:
    """In public mode, instance-wide data needs the admin token
    (X-Admin-Token header or ?admin_token=)."""
    if not settings.public_mode:
        return
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin token required")


def _require_admin_token(request: Request) -> None:
    """Always require the admin token (used for admin-only controls like the
    invite system, regardless of public/private mode)."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin token required")


# ── Pages ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _page_ctx())


@app.get("/monitors", response_class=HTMLResponse)
async def monitors_page(request: Request):
    return templates.TemplateResponse(request, "monitors.html", _page_ctx())


@app.get("/m/{manage_token}", response_class=HTMLResponse)
async def manage_page(request: Request, manage_token: str):
    if db.get_monitor_by_token(manage_token) is None:
        raise HTTPException(status_code=404, detail="Unknown management link")
    return templates.TemplateResponse(
        request, "manage.html", {**_page_ctx(), "manage_token": manage_token})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse(request, "privacy.html", _page_ctx())


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    # The page itself is just a shell — all data behind it comes from the
    # admin-token-gated API, so rendering it to anyone is harmless.
    return templates.TemplateResponse(request, "admin.html", _page_ctx())


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "monitors": db.count_monitors(),
        "active_monitors": db.count_active_monitors(),
        "effective_interval_seconds": scheduler.effective_interval(),
        "telegram": bot.enabled() and bot.bot_username is not None,
    }


# ── On-demand checks ──────────────────────────────────────────────

class CheckIn(BaseModel):
    expediente_id: str = Field(min_length=1, max_length=25)
    fecha_presentacion: str
    anio_nacimiento: str
    turnstile_token: str = ""


def _to_request(body: CheckIn) -> CheckRequest:
    request = CheckRequest(
        expediente_id=body.expediente_id.strip().upper(),
        fecha_presentacion=body.fecha_presentacion.strip(),
        anio_nacimiento=body.anio_nacimiento.strip(),
    )
    errors = request.validate()
    if errors:
        raise HTTPException(status_code=422, detail=" ".join(errors))
    return request


@app.post("/api/checks")
async def create_check(body: CheckIn, request: Request):
    check_request = _to_request(body)
    ip = client_ip(request)

    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(status_code=403, detail="Bot check failed — reload and try again")

    # Cached result from the last few minutes: answer instantly, spend nothing.
    cached = result_cache.get(check_request.expediente_id,
                              check_request.fecha_presentacion,
                              check_request.anio_nacimiento)
    if cached:
        return {"id": "cached", "status": "done", "progress": "Done",
                "error": None, "expediente_id": check_request.expediente_id,
                "fields": cached.fields, "checked_at": cached.checked_at.isoformat(),
                "cached": True}

    # Identical lookup already queued/running: share it instead of queueing twice.
    existing = queue.find_active(check_request)
    if existing:
        return existing.public_view(position=queue.position(existing))

    if settings.public_mode and not rate_limiter.allow(
            "checks", ip, settings.rate_limit_checks_per_hour, 3600):
        raise HTTPException(status_code=429,
                            detail="Too many checks from your address — try again later")

    job = queue.submit(check_request)
    return job.public_view(position=queue.position(job))


@app.get("/api/checks/{job_id}")
async def get_check(job_id: str):
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job.public_view(position=queue.position(job))


# ── Monitors ──────────────────────────────────────────────────────

class MonitorIn(CheckIn):
    label: str = Field(default="", max_length=60)
    invite_code: str = Field(default="", max_length=12)


def _telegram_link(monitor_id: int, is_creator: bool = False) -> dict | None:
    """Fresh one-time activation link for a monitor, or None if the bot
    isn't available (no token / username not resolved yet)."""
    if not bot.enabled() or bot.bot_username is None:
        return None
    link = db.create_link_code(monitor_id, is_creator=is_creator)
    return {**link, "deep_link": bot.deep_link(link["code"])}


@app.get("/api/telegram/status")
async def telegram_status():
    return {"enabled": bot.enabled(), "bot_username": bot.bot_username}


@app.get("/api/monitors")
async def list_monitors(request: Request):
    _require_admin(request)
    monitors = db.list_monitors()
    counts = db.subscriber_counts()
    for m in monitors:
        m["check_queued"] = queue.is_monitor_queued(m["id"])
        m["subscribers"] = counts.get(m["id"], 0)
    return monitors


@app.post("/api/monitors", status_code=201)
async def add_monitor(body: MonitorIn, request: Request):
    check_request = _to_request(body)
    ip = client_ip(request)

    admin = _is_admin(request)

    if not admin and not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(status_code=403, detail="Bot check failed — reload and try again")
    if settings.public_mode and not admin:
        if not rate_limiter.allow("monitors", ip,
                                  settings.rate_limit_monitors_per_day, 86400):
            raise HTTPException(status_code=429,
                                detail="Too many monitors created from your address today")
        if settings.max_monitors and db.count_monitors() >= settings.max_monitors:
            raise HTTPException(
                status_code=503,
                detail="This instance is at capacity and can't accept new monitors "
                       "right now. You can still use one-off checks, or self-host "
                       "your own instance (see the project README).")

    # Invitation gate: a valid one-time code is consumed atomically. The admin
    # (with token) bypasses it. If monitor creation then fails, the code is
    # refunded so it isn't burned.
    invite_required = invite_only_enabled() and not admin
    if invite_required:
        if not db.consume_invite_code(body.invite_code, check_request.expediente_id):
            raise HTTPException(
                status_code=403,
                detail="A valid invitation code is required to create a monitor.")

    try:
        monitor_id = db.add_monitor(
            label=body.label.strip(),
            expediente_id=check_request.expediente_id,
            fecha_presentacion=check_request.fecha_presentacion,
            anio_nacimiento=check_request.anio_nacimiento,
        )
    except Exception:
        if invite_required:
            db.refund_invite_code(body.invite_code)
        raise HTTPException(status_code=409, detail="This expediente is already monitored")
    # First check runs immediately (scheduler would also pick it up within a minute).
    try:
        queue.submit(check_request, monitor_id=monitor_id)
    except ValueError:
        pass
    monitor = db.get_monitor(monitor_id)
    monitor["telegram_link"] = _telegram_link(monitor_id, is_creator=True)
    monitor["manage_url"] = f"/m/{monitor['manage_token']}"
    return monitor


@app.post("/api/monitors/{monitor_id}/link-code")
async def monitor_link_code(monitor_id: int, request: Request):
    _require_admin(request)
    if db.get_monitor(monitor_id) is None:
        raise HTTPException(status_code=404, detail="Unknown monitor")
    link = _telegram_link(monitor_id)
    if link is None:
        raise HTTPException(status_code=409,
                            detail="Telegram bot is not configured (set TELEGRAM_TOKEN)")
    return link


@app.get("/api/telegram/qr.png")
async def telegram_qr(code: str):
    """QR for the t.me activation deep link (content is only ever a t.me URL
    for our own bot, so an arbitrary `code` can't produce a hostile QR)."""
    link = bot.deep_link(code)
    if link is None:
        raise HTTPException(status_code=409, detail="Telegram bot is not configured")
    import io

    import segno
    buf = io.BytesIO()
    segno.make(link).save(buf, kind="png", scale=5, border=2)
    return Response(content=buf.getvalue(), media_type="image/png")


class CancelIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


@app.post("/api/subscriptions/cancel")
async def cancel_subscription(body: CancelIn):
    if not db.cancel_subscription_by_code(body.code):
        raise HTTPException(status_code=404, detail="Unknown or already-used cancellation code")


# ── Check frequency (admin) ───────────────────────────────────────

# Presets offered in the UI; any value in [min, max] is accepted server-side.
INTERVAL_PRESETS = [
    {"label": "1 hour", "seconds": 3600},
    {"label": "2 hours", "seconds": 7200},
    {"label": "4 hours", "seconds": 14400},
    {"label": "6 hours", "seconds": 21600},
    {"label": "8 hours", "seconds": 28800},
    {"label": "12 hours", "seconds": 43200},
    {"label": "24 hours", "seconds": 86400},
]
INTERVAL_MIN = 1800       # 30 min floor — below this the WAF risk climbs
INTERVAL_MAX = 604800     # 7 days


@app.get("/api/admin/settings")
async def admin_settings(request: Request):
    _require_admin_token(request)
    return {
        "base_interval_seconds": scheduler.base_interval(),
        "effective_interval_seconds": scheduler.effective_interval(),
        "presets": INTERVAL_PRESETS,
        "min_seconds": INTERVAL_MIN,
        "max_seconds": INTERVAL_MAX,
    }


class IntervalIn(BaseModel):
    seconds: int


@app.post("/api/admin/monitor-interval")
async def admin_set_interval(body: IntervalIn, request: Request):
    _require_admin_token(request)
    if not (INTERVAL_MIN <= body.seconds <= INTERVAL_MAX):
        raise HTTPException(
            status_code=422,
            detail=f"Interval must be between {INTERVAL_MIN // 60} minutes "
                   f"and {INTERVAL_MAX // 86400} days.")
    db.set_int_setting("monitor_interval_seconds", body.seconds)
    log.info("Monitor base interval set to %ss via admin", body.seconds)
    return {
        "base_interval_seconds": body.seconds,
        "effective_interval_seconds": scheduler.effective_interval(),
    }


# ── Invitation system (admin) ─────────────────────────────────────

@app.get("/api/admin/invites")
async def admin_invites(request: Request):
    _require_admin_token(request)
    return {
        "invite_only": invite_only_enabled(),
        "counts": db.invite_counts(),
        "codes": db.list_invite_codes(),
    }


class InviteModeIn(BaseModel):
    enabled: bool


@app.post("/api/admin/invite-mode")
async def admin_invite_mode(body: InviteModeIn, request: Request):
    _require_admin_token(request)
    db.set_flag("invite_only", body.enabled)
    log.info("Invite-only mode set to %s via admin", body.enabled)
    return {"invite_only": body.enabled}


@app.post("/api/admin/invites")
async def admin_create_invites(request: Request, count: int = 1):
    _require_admin_token(request)
    count = max(1, min(count, 50))
    codes = [db.create_invite_code() for _ in range(count)]
    return {"created": codes}


@app.delete("/api/admin/invites/{code}", status_code=204)
async def admin_delete_invite(code: str, request: Request):
    _require_admin_token(request)
    db.delete_invite_code(code)
    return {"cancelled": True}


@app.delete("/api/monitors/{monitor_id}", status_code=204)
async def delete_monitor(monitor_id: int, request: Request):
    _require_admin(request)
    monitor = db.get_monitor(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Unknown monitor")
    log.info("Monitor %s (%s) deleted via admin API from %s",
             monitor_id, monitor["expediente_id"], client_ip(request))
    db.delete_monitor(monitor_id)


@app.post("/api/monitors/{monitor_id}/check-now")
async def check_monitor_now(monitor_id: int, request: Request):
    _require_admin(request)
    monitor = db.get_monitor(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Unknown monitor")
    return _submit_monitor_check(monitor)


@app.get("/api/monitors/{monitor_id}/history")
async def monitor_history(monitor_id: int, request: Request):
    _require_admin(request)
    monitor = db.get_monitor(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Unknown monitor")
    return db.recent_history(monitor["expediente_id"])


# ── Management by capability token (/m/<token>) ───────────────────

def _monitor_or_404(manage_token: str) -> dict:
    monitor = db.get_monitor_by_token(manage_token)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Unknown management link")
    return monitor


def _submit_monitor_check(monitor: dict) -> dict:
    request = CheckRequest(
        expediente_id=monitor["expediente_id"],
        fecha_presentacion=monitor["fecha_presentacion"],
        anio_nacimiento=monitor["anio_nacimiento"],
    )
    try:
        job = queue.submit(request, monitor_id=monitor["id"])
    except ValueError:
        raise HTTPException(status_code=409, detail="A check for this monitor is already queued")
    return job.public_view(position=queue.position(job))


def _masked_subscribers(monitor_id: int) -> list[dict]:
    """Subscribers as shown on the management page — no raw chat ids."""
    return [{
        "id": s["id"],
        "name": s["chat_name"] or "subscriber",
        "channel": s["channel"],
        "since": s["created_at"],
    } for s in db.subscriptions_for_monitor(monitor_id)]


@app.get("/api/manage/{manage_token}")
async def manage_view(manage_token: str):
    monitor = _monitor_or_404(manage_token)
    return {
        "label": monitor["label"],
        "expediente_id": monitor["expediente_id"],
        "fecha_presentacion": monitor["fecha_presentacion"],
        "paused": bool(monitor["paused"]),
        "last_checked_at": monitor["last_checked_at"],
        "last_state": monitor["last_state"],
        "last_error": monitor["last_error"],
        "check_queued": queue.is_monitor_queued(monitor["id"]),
        "subscribers": _masked_subscribers(monitor["id"]),
        "history": db.recent_history(monitor["expediente_id"], limit=10),
        "interval_seconds": monitor["interval_seconds"],  # None → follows global base
        "effective_interval_seconds": scheduler.monitor_interval(monitor),
        "base_interval_seconds": scheduler.base_interval(),
        "interval_presets": INTERVAL_PRESETS,
    }


class ManageUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=60)
    paused: bool | None = None
    # 0 clears the override (follow the global base); a positive value sets a
    # per-monitor interval; None (default) leaves it unchanged.
    interval_seconds: int | None = None


@app.patch("/api/manage/{manage_token}")
async def manage_update(manage_token: str, body: ManageUpdate):
    monitor = _monitor_or_404(manage_token)
    if body.label is not None:
        db.set_monitor_label(monitor["id"], body.label.strip())
    if body.paused is not None:
        db.set_monitor_paused(monitor["id"], body.paused)
    if body.interval_seconds is not None:
        if body.interval_seconds == 0:
            db.set_monitor_interval(monitor["id"], None)  # follow global base
        elif INTERVAL_MIN <= body.interval_seconds <= INTERVAL_MAX:
            db.set_monitor_interval(monitor["id"], body.interval_seconds)
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Interval must be between {INTERVAL_MIN // 60} minutes "
                       f"and {INTERVAL_MAX // 86400} days (or 0 to follow the default).")
    return await manage_view(manage_token)


@app.post("/api/manage/{manage_token}/check-now")
async def manage_check_now(manage_token: str):
    return _submit_monitor_check(_monitor_or_404(manage_token))


@app.post("/api/manage/{manage_token}/link-code")
async def manage_link_code(manage_token: str):
    monitor = _monitor_or_404(manage_token)
    link = _telegram_link(monitor["id"])
    if link is None:
        raise HTTPException(status_code=409,
                            detail="Telegram bot is not configured on this instance")
    return link


@app.delete("/api/manage/{manage_token}/subscriptions/{subscription_id}", status_code=204)
async def manage_remove_subscriber(manage_token: str, subscription_id: int, request: Request):
    monitor = _monitor_or_404(manage_token)
    if not any(s["id"] == subscription_id
               for s in db.subscriptions_for_monitor(monitor["id"])):
        raise HTTPException(status_code=404, detail="Unknown subscriber")
    log.info("Subscription %s removed from monitor %s via manage page from %s",
             subscription_id, monitor["id"], client_ip(request))
    db.delete_subscription(subscription_id)


@app.delete("/api/manage/{manage_token}", status_code=204)
async def manage_delete(manage_token: str, request: Request):
    monitor = _monitor_or_404(manage_token)
    log.info("Monitor %s (%s) deleted via manage page from %s",
             monitor["id"], monitor["expediente_id"], client_ip(request))
    db.delete_monitor(monitor["id"])


def main() -> None:
    import uvicorn
    uvicorn.run("app.web.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
