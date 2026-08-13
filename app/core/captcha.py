import asyncio
import base64
import io
import logging

import httpx
import pytesseract
from PIL import Image, ImageFilter

log = logging.getLogger(__name__)

CAPTCHA_LEN = 5


class CaptchaSolver:
    """Solves the site's 5-character image captcha. Return the guessed text;
    callers treat a wrong-length or rejected answer as a soft failure and retry
    with a fresh image."""

    name = "base"

    async def solve(self, image_bytes: bytes) -> str:
        raise NotImplementedError


class OcrSolver(CaptchaSolver):
    """Free local solver: Tesseract OCR. Succeeds roughly 55-60% of runs."""

    name = "ocr"

    async def solve(self, image_bytes: bytes) -> str:
        return await asyncio.to_thread(self._solve_sync, image_bytes)

    @staticmethod
    def _solve_sync(image_bytes: bytes) -> str:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        w, h = img.size
        img = img.resize((w * 4, h * 4), Image.LANCZOS)
        img = img.point(lambda x: 0 if x < 100 else 255)
        # The site draws a thin strikethrough line across the text; opening
        # (erode+dilate) removes lines thinner than the letter strokes.
        img = img.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
        text = pytesseract.image_to_string(
            img,
            config="--psm 7 --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz0123456789",
        )
        return text.strip().lower().replace(" ", "").replace("\n", "")


class TwoCaptchaSolver(CaptchaSolver):
    """2captcha.com paid API (classic in.php/res.php endpoints)."""

    name = "2captcha"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def solve(self, image_bytes: bytes) -> str:
        b64 = base64.b64encode(image_bytes).decode()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://2captcha.com/in.php",
                data={
                    "key": self.api_key,
                    "method": "base64",
                    "body": b64,
                    "min_len": CAPTCHA_LEN,
                    "max_len": CAPTCHA_LEN,
                    "json": 1,
                },
            )
            data = resp.json()
            if data.get("status") != 1:
                raise RuntimeError(f"2captcha submit failed: {data.get('request')}")
            task_id = data["request"]

            for _ in range(24):  # up to ~2 minutes
                await asyncio.sleep(5)
                resp = await client.get(
                    "https://2captcha.com/res.php",
                    params={"key": self.api_key, "action": "get", "id": task_id, "json": 1},
                )
                data = resp.json()
                if data.get("status") == 1:
                    return data["request"].strip().lower()
                if data.get("request") != "CAPCHA_NOT_READY":
                    raise RuntimeError(f"2captcha error: {data.get('request')}")
        raise RuntimeError("2captcha timed out")


class AntiCaptchaSolver(CaptchaSolver):
    """anti-captcha.com paid API (ImageToTextTask)."""

    name = "anticaptcha"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def solve(self, image_bytes: bytes) -> str:
        b64 = base64.b64encode(image_bytes).decode()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anti-captcha.com/createTask",
                json={
                    "clientKey": self.api_key,
                    "task": {"type": "ImageToTextTask", "body": b64, "case": False},
                },
            )
            data = resp.json()
            if data.get("errorId"):
                raise RuntimeError(f"anti-captcha submit failed: {data.get('errorDescription')}")
            task_id = data["taskId"]

            for _ in range(24):
                await asyncio.sleep(5)
                resp = await client.post(
                    "https://api.anti-captcha.com/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                )
                data = resp.json()
                if data.get("errorId"):
                    raise RuntimeError(f"anti-captcha error: {data.get('errorDescription')}")
                if data.get("status") == "ready":
                    return data["solution"]["text"].strip().lower()
        raise RuntimeError("anti-captcha timed out")


class FallbackSolver(CaptchaSolver):
    """Try the primary solver; if it raises (paid API down, no balance, network
    error), fall back to the secondary so a check still gets an attempt."""

    def __init__(self, primary: CaptchaSolver, fallback: CaptchaSolver):
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+{fallback.name}"

    async def solve(self, image_bytes: bytes) -> str:
        try:
            return await self.primary.solve(image_bytes)
        except Exception as e:
            log.warning("Primary captcha solver '%s' failed (%s) — falling back to '%s'",
                        self.primary.name, e, self.fallback.name)
            return await self.fallback.solve(image_bytes)


def _build_one(provider: str, api_key: str = "") -> CaptchaSolver:
    provider = provider.strip().lower()
    if provider == "ocr":
        return OcrSolver()
    if provider in ("2captcha", "twocaptcha"):
        if not api_key:
            raise ValueError("CAPTCHA_API_KEY is required for the 2captcha provider")
        return TwoCaptchaSolver(api_key)
    if provider in ("anticaptcha", "anti-captcha"):
        if not api_key:
            raise ValueError("CAPTCHA_API_KEY is required for the anticaptcha provider")
        return AntiCaptchaSolver(api_key)
    raise ValueError(f"Unknown CAPTCHA_PROVIDER '{provider}' (use ocr, 2captcha or anticaptcha)")


def build_solver(provider: str, api_key: str = "", ocr_fallback: bool = True) -> CaptchaSolver:
    """Build the configured solver. When a paid provider is selected and
    ocr_fallback is on, wrap it so failures degrade to the free OCR solver
    rather than failing the whole check."""
    primary = _build_one(provider, api_key)
    if ocr_fallback and not isinstance(primary, OcrSolver):
        return FallbackSolver(primary, OcrSolver())
    return primary
