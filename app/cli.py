"""One-off status check without the web UI:

    python -m app.cli check <expediente_id> <fecha DD/MM/YYYY> <año_nacimiento>
"""

import asyncio
import logging
import sys

from .config import settings
from .core.browser import StatusChecker
from .core.captcha import build_solver
from .core.models import CheckError, CheckRequest


async def _progress(msg: str) -> None:
    print(f"  … {msg}")


async def run(request: CheckRequest) -> int:
    checker = StatusChecker(
        base_url=settings.base_url,
        solver=build_solver(settings.captcha_provider, settings.captcha_api_key,
                            settings.captcha_ocr_fallback),
        max_captcha_attempts=settings.captcha_max_attempts,
        debug_dir=settings.debug_dir if settings.debug_screenshots else None,
    )
    try:
        result = await checker.check(request, progress=_progress, debug_label="cli")
    except CheckError as e:
        print(f"FAILED: {e}")
        return 1

    print()
    for key, value in result.fields.items():
        print(f"  {key}: {value or '—'}")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    args = sys.argv[1:]
    if len(args) != 4 or args[0] != "check":
        print(__doc__.strip())
        raise SystemExit(2)

    request = CheckRequest(
        expediente_id=args[1].strip().upper(),
        fecha_presentacion=args[2].strip(),
        anio_nacimiento=args[3].strip(),
    )
    errors = request.validate()
    if errors:
        for err in errors:
            print(f"error: {err}")
        raise SystemExit(2)

    raise SystemExit(asyncio.run(run(request)))


if __name__ == "__main__":
    main()
