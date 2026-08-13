import asyncio

import pytest

from app.core.captcha import (
    AntiCaptchaSolver,
    CaptchaSolver,
    FallbackSolver,
    OcrSolver,
    TwoCaptchaSolver,
    build_solver,
)


def test_default_ocr():
    assert isinstance(build_solver("ocr"), OcrSolver)


def test_paid_providers_need_key():
    with pytest.raises(ValueError):
        build_solver("2captcha")
    with pytest.raises(ValueError):
        build_solver("anticaptcha")


def test_paid_providers_with_key():
    assert isinstance(build_solver("2captcha", "k", ocr_fallback=False), TwoCaptchaSolver)
    assert isinstance(build_solver("anti-captcha", "k", ocr_fallback=False), AntiCaptchaSolver)


def test_unknown_provider():
    with pytest.raises(ValueError):
        build_solver("magic")


def test_paid_provider_wrapped_with_ocr_fallback():
    solver = build_solver("2captcha", "k", ocr_fallback=True)
    assert isinstance(solver, FallbackSolver)
    assert isinstance(solver.fallback, OcrSolver)


def test_ocr_provider_not_wrapped():
    assert isinstance(build_solver("ocr", ocr_fallback=True), OcrSolver)


def test_fallback_used_when_primary_raises():
    class Boom(CaptchaSolver):
        name = "boom"
        async def solve(self, image_bytes):
            raise RuntimeError("no balance")

    class Fixed(CaptchaSolver):
        name = "fixed"
        async def solve(self, image_bytes):
            return "abc12"

    solver = FallbackSolver(Boom(), Fixed())
    assert asyncio.run(solver.solve(b"x")) == "abc12"
