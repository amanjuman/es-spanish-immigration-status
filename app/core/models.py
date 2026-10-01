import re
from dataclasses import dataclass, field
from datetime import datetime


EXPEDIENTE_RE = re.compile(r"^[A-Za-z0-9]{5,25}$")
FECHA_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
ANIO_RE = re.compile(r"^(19|20)\d{2}$")
# N.I.E. (foreigner ID): X/Y/Z + 7 digits + control letter. The portal's
# expediente field rejects these ("El número de expediente introducido no es
# válido"), so catch them up front instead of burning captchas.
NIE_RE = re.compile(r"^[XYZ]\d{7}[A-Z]$", re.IGNORECASE)


@dataclass(frozen=True)
class CheckRequest:
    """One status lookup: expediente id, presentation date (DD/MM/YYYY),
    applicant's birth year."""

    expediente_id: str
    fecha_presentacion: str
    anio_nacimiento: str

    def validate(self) -> list[str]:
        errors = []
        if NIE_RE.match(self.expediente_id):
            errors.append(
                "That looks like an N.I.E., which the portal doesn't accept here. "
                "Enter the expediente / solicitud number from your application "
                "receipt instead (e.g. E28… or a 15-digit number).")
        elif not EXPEDIENTE_RE.match(self.expediente_id):
            errors.append("Expediente id must be 5-25 letters/digits.")
        if not FECHA_RE.match(self.fecha_presentacion):
            errors.append("Fecha de presentación must be DD/MM/YYYY.")
        else:
            try:
                datetime.strptime(self.fecha_presentacion, "%d/%m/%Y")
            except ValueError:
                errors.append("Fecha de presentación is not a real date.")
        if not ANIO_RE.match(self.anio_nacimiento):
            errors.append("Año de nacimiento must be a 4-digit year.")
        return errors


@dataclass
class CheckResult:
    """Parsed outcome of a successful lookup."""

    fields: dict[str, str] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=datetime.now)

    @property
    def nie(self) -> str:
        return self.fields.get("N.I.E", "")

    @property
    def estado(self) -> str:
        return self.fields.get("Estado de Resolución", "")

    @property
    def fecha_resolucion(self) -> str:
        return self.fields.get("Fecha de Resolución", "")

    def state_key(self) -> dict[str, str]:
        """The subset of fields whose change should trigger a notification."""
        return {
            "nie": self.nie,
            "estado": self.estado,
            "fecha_resolucion": self.fecha_resolucion,
        }


class CheckError(Exception):
    """Base class; message is safe to show to the user. `code` lets callers
    react to the kind of failure (e.g. pause a monitor with bad input)."""

    code = "check_error"


class WafBlockedError(CheckError):
    code = "waf"

    def __init__(self, detail: str = ""):
        super().__init__(
            "The government site's firewall blocked this attempt"
            + (f" ({detail})" if detail else "")
            + ". Wait a while before retrying."
        )


class CaptchaExhaustedError(CheckError):
    code = "captcha"

    def __init__(self, attempts: int):
        super().__init__(
            f"Could not solve the captcha after {attempts} attempts. "
            "Try again, or configure a paid captcha provider for reliability."
        )


class PageFlowError(CheckError):
    """The site did not behave as expected (layout change, missing element)."""

    code = "page_flow"


class InvalidInputError(CheckError):
    """The portal rejected the submitted details themselves (e.g. "El número
    de expediente introducido no es válido"). Retrying with a new captcha can
    never help, so callers should stop — and a monitor should be paused."""

    code = "invalid_input"

    def __init__(self, portal_message: str):
        self.portal_message = portal_message
        super().__init__(
            f'The portal rejected these details: "{portal_message}". '
            "Check the expediente / solicitud number, presentation date and "
            "birth year. An N.I.E. can't be used here — use the number from "
            "your application receipt."
        )
