import logging

import httpx

from ..config import settings

log = logging.getLogger(__name__)


async def send(channel: str, address: str, message: str) -> bool:
    """Channel-agnostic dispatch. `address` is a chat id for telegram, a
    phone number for whatsapp (not yet implemented — requires a Meta Cloud
    API / Twilio account and approved message templates)."""
    if channel == "telegram":
        return await send_telegram(message, chat_id=address)
    if channel == "whatsapp":
        log.warning("WhatsApp channel not implemented yet — dropping alert for %s", address)
        return False
    log.error("Unknown notification channel '%s'", channel)
    return False


async def send_telegram(message: str, chat_id: str = "") -> bool:
    """Send an HTML-formatted Telegram message. Returns False (and logs) on
    any failure — notification problems must never crash a check."""
    token = settings.telegram_token
    chat_id = chat_id or settings.telegram_chat_id
    if not token or not chat_id:
        log.info("Telegram not configured — skipping notification")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            )
        if resp.is_success:
            log.info("Telegram notification sent")
            return True
        log.error("Telegram API error: %s", resp.text)
    except Exception:
        log.exception("Telegram send failed")
    return False
