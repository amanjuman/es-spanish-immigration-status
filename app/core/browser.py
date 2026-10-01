"""Playwright automation of the infoext2 status-lookup flow.

The government site sits behind an aggressive F5 WAF. All the pacing
(sleeps, few widely-spaced captcha retries, stealth context) exists to stay
below its bot-detection threshold — do not "optimise" the waits away.
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import Page, async_playwright
from playwright_stealth import Stealth

from .captcha import CAPTCHA_LEN, CaptchaSolver
from .models import (
    CaptchaExhaustedError,
    CheckRequest,
    CheckResult,
    InvalidInputError,
    PageFlowError,
    WafBlockedError,
)
from .parser import extract_result_fields

log = logging.getLogger(__name__)

MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

ProgressFn = Callable[[str], Awaitable[None]]


async def _noop_progress(_: str) -> None:
    return None


class StatusChecker:
    def __init__(
        self,
        base_url: str,
        solver: CaptchaSolver,
        max_captcha_attempts: int = 5,
        debug_dir: Path | None = None,
    ):
        self.base_url = base_url
        self.solver = solver
        self.max_captcha_attempts = max_captcha_attempts
        self.debug_dir = debug_dir

    async def check(
        self,
        request: CheckRequest,
        progress: ProgressFn = _noop_progress,
        debug_label: str = "check",
    ) -> CheckResult:
        """Run one full lookup. Raises WafBlockedError / CaptchaExhaustedError /
        PageFlowError on failure."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                    "--window-size=1920,1080",
                ],
            )
            try:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 KHTML, like Gecko Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="es-ES",
                    timezone_id="Europe/Madrid",
                    extra_http_headers={
                        "Accept-Language": "es-ES,es;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Connection": "keep-alive",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)
                html = await self._run_flow(page, request, progress, debug_label)
            finally:
                await browser.close()

        fields = extract_result_fields(html)
        log.info("Parsed fields for %s: %s", request.expediente_id, sorted(fields))
        return CheckResult(fields=fields)

    async def _debug_shot(self, page: Page, label: str, step: str) -> None:
        if self.debug_dir:
            try:
                await page.screenshot(path=str(self.debug_dir / f"{label}_{step}.png"))
            except Exception:
                log.exception("debug screenshot failed")

    async def _fill_form(self, page: Page, request: CheckRequest) -> None:
        # The form has two lookup modes; cambiaExpte(m) shows the matching row
        # and sets the hidden "modo" field: 'N' = by N.I.E. (the portal's
        # default, input "nie"), 'X' = by expediente/solicitud number (input
        # "idExpediente"). Always set it explicitly — recargarCaptcha() reloads
        # the whole form back to its default.
        mode = request.lookup_mode
        await page.evaluate(f"cambiaExpte('{mode}')")
        await asyncio.sleep(1)
        field = "nie" if mode == "N" else "idExpediente"
        await page.fill(f"input[name='{field}']", request.expediente_id)
        await self._set_fecha(page, request.fecha_presentacion)
        await page.fill("input[name='anio']", request.anio_nacimiento)

    async def _set_fecha(self, page: Page, fecha_str: str) -> None:
        """The site's date field is a custom calendar widget — typing text into it
        does not update its internal selection, only clicking a day cell does."""
        day, month, year = fecha_str.split("/")
        day = str(int(day))
        target_month = int(month) - 1
        target_year = int(year)

        fecha_input = page.locator("input[name='fechaPresentacion']")
        await fecha_input.click()
        await asyncio.sleep(0.5)
        popup = page.locator(".datepicker:visible").last

        for _ in range(36):
            header = await popup.locator(".datepickerMonth span").inner_text()
            mes_txt, anio_txt = [s.strip() for s in header.split(",")]
            cur_month = MESES_ES.index(mes_txt.lower())
            cur_year = int(anio_txt)
            if cur_month == target_month and cur_year == target_year:
                break
            cur_idx = cur_year * 12 + cur_month
            tgt_idx = target_year * 12 + target_month
            if cur_idx > tgt_idx:
                await popup.locator(".datepickerGoPrev").click()
            else:
                await popup.locator(".datepickerGoNext").click()
            await asyncio.sleep(0.2)
        else:
            raise PageFlowError("Could not navigate datepicker to target month/year")

        day_cell = popup.locator(
            "tbody.datepickerDays td:not(.datepickerNotInMonth) span",
            has_text=re.compile(rf"^{day}$"),
        ).first
        await day_cell.click()

    @staticmethod
    def _waf_verdict(html: str) -> str | None:
        lowered = html.lower()
        if "rejected" in lowered:
            return "request rejected"
        if "human visitor" in lowered:
            return "bot-challenge page"
        return None

    async def _run_flow(
        self, page: Page, request: CheckRequest, progress: ProgressFn, debug_label: str
    ) -> str:
        await progress("Loading government site…")
        await page.goto(self.base_url, wait_until="networkidle")
        await asyncio.sleep(4)

        await page.evaluate("document.getElementById('frmFormu').submit()")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(3)

        html = await page.content()
        if verdict := self._waf_verdict(html):
            await self._debug_shot(page, debug_label, "error")
            raise WafBlockedError(verdict)

        await progress("Waiting for captcha…")
        captcha_ready = False
        for _ in range(10):
            await asyncio.sleep(1)
            dims = await page.evaluate("""
                () => {
                    const img = document.querySelector("img[alt='captcha']");
                    return img ? { w: img.naturalWidth, h: img.naturalHeight } : null;
                }
            """)
            if dims and dims["w"] > 10:
                captcha_ready = True
                break
        if not captcha_ready:
            await self._debug_shot(page, debug_label, "error")
            raise PageFlowError("Captcha image never rendered")

        await progress("Filling the form…")
        await self._fill_form(page, request)

        # The site validates the captcha only on full submission (no client-side
        # pre-check), and a wrong guess just redisplays the same page with a
        # fresh captcha image and an error banner — so retry in place.
        # Keep attempts few and spaced out: hammering resubmissions quickly
        # escalates the F5 WAF into its own bot-challenge page (TSPD captcha),
        # which is worse than the site's normal image captcha and not solvable here.
        last_failure = "captcha"   # what ended the most recent failed attempt
        unexpected_streak = 0      # consecutive "unexpected page" outcomes
        for attempt in range(1, self.max_captcha_attempts + 1):
            if attempt > 1:
                await asyncio.sleep(6)

            content_now = await page.content()
            if verdict := self._waf_verdict(content_now):
                await self._debug_shot(page, debug_label, "error")
                raise WafBlockedError(verdict)

            captcha_el = await page.query_selector("img[alt='captcha']")
            if not captcha_el:
                log.warning("Captcha element missing on attempt %d", attempt)
                continue
            captcha_bytes = await captcha_el.screenshot()
            if self.debug_dir:
                (self.debug_dir / f"{debug_label}_captcha.png").write_bytes(captcha_bytes)
            await progress(f"Solving captcha (attempt {attempt}/{self.max_captcha_attempts})…")
            try:
                captcha_text = await self.solver.solve(captcha_bytes)
            except Exception as e:
                log.warning("Captcha solver (%s) failed: %s", self.solver.name, e)
                captcha_text = ""
            log.info("Captcha solved as: '%s'", captcha_text)

            if len(captcha_text) != CAPTCHA_LEN:
                log.info("Wrong-length captcha '%s' on attempt %d — reloading", captcha_text, attempt)
                # recargarCaptcha() resets the whole form (mode + fields), not just the image.
                await page.evaluate("recargarCaptcha()")
                await asyncio.sleep(2)
                reload_html = await page.content()
                if "cambiaExpte" not in reload_html:
                    await self._debug_shot(page, debug_label, "error")
                    raise PageFlowError("Page lost its normal JS after a captcha reload")
                await self._fill_form(page, request)
                continue

            await page.fill("input[name='txtCaptcha']", captcha_text)
            await self._debug_shot(page, debug_label, "filled")

            await progress(f"Submitting query (attempt {attempt}/{self.max_captcha_attempts})…")
            await page.evaluate("envioForm()")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)

            html = await page.content()
            if verdict := self._waf_verdict(html):
                await self._debug_shot(page, debug_label, "error")
                raise WafBlockedError(verdict)

            if "caracteres escritos no son correctos" in html.lower():
                log.info("Captcha '%s' rejected by server — retrying", captcha_text)
                last_failure = "captcha"
                unexpected_streak = 0
                continue

            if "datos personales" in html.lower():
                await self._debug_shot(page, debug_label, "result")
                return html

            # The portal rejected the details themselves (e.g. a malformed or
            # unknown ID). A new captcha can never fix that, so
            # stop now instead of burning every remaining attempt.
            if portal_msg := await self._visible_validation_error(page):
                log.warning("Portal rejected the submitted details: %s", portal_msg)
                await self._debug_shot(page, debug_label, "invalid")
                raise InvalidInputError(portal_msg)

            log.warning("Unexpected page state on attempt %d — retrying", attempt)
            await self._debug_shot(page, debug_label, "unexpected")
            last_failure = "unexpected"
            unexpected_streak += 1
            # An unexpected page is rarely fixed by a fresh captcha, and each
            # retry costs a solve — give up after two in a row.
            if unexpected_streak >= 2:
                raise PageFlowError(
                    "The portal returned an unexpected page after the form was "
                    "submitted (this was not a captcha error). It may be "
                    "temporarily unavailable, or its layout may have changed.")
            await self._fill_form(page, request)

        if last_failure == "unexpected":
            raise PageFlowError(
                "The portal returned an unexpected page after the form was "
                "submitted (this was not a captcha error).")
        raise CaptchaExhaustedError(self.max_captcha_attempts)

    @staticmethod
    async def _visible_validation_error(page: Page) -> str | None:
        """The portal's visible field-validation message, if it rejected the
        submitted details. Checked by visibility (not raw HTML) so a hidden
        error template on a normal page can't cause a false match."""
        matches = page.get_by_text(re.compile(r"no es v[aá]lid", re.IGNORECASE))
        for i in range(await matches.count()):
            el = matches.nth(i)
            if await el.is_visible():
                text = " ".join((await el.inner_text()).split())
                return text[:200]
        return None
