from app.core.models import CheckRequest, CheckResult


def valid_request(**overrides):
    data = dict(
        expediente_id="E28202600000000",
        fecha_presentacion="03/06/2026",
        anio_nacimiento="1990",
    )
    data.update(overrides)
    return CheckRequest(**data)


def test_valid_request_passes():
    assert valid_request().validate() == []


def test_nie_accepted_and_uses_nie_mode():
    for nie in ("Z1234567X", "x1234567l", "Y7654321B"):
        req = valid_request(expediente_id=nie)
        assert req.validate() == []
        assert req.lookup_mode == "N"


def test_expediente_formats_use_expediente_mode():
    for exp in ("E28202600000001", "I28202600000002", "287020260000003"):
        req = valid_request(expediente_id=exp)
        assert req.validate() == []
        assert req.lookup_mode == "X"


def test_bad_expediente():
    assert valid_request(expediente_id="E28 111!").validate()
    assert valid_request(expediente_id="").validate()


def test_bad_fecha():
    assert valid_request(fecha_presentacion="2026-06-03").validate()
    assert valid_request(fecha_presentacion="32/13/2026").validate()


def test_bad_anio():
    assert valid_request(anio_nacimiento="90").validate()
    assert valid_request(anio_nacimiento="1850").validate()


def test_state_key():
    result = CheckResult(fields={
        "N.I.E": "Z9999999R",
        "Estado de Resolución": "EN TRAMITE",
        "Fecha de Resolución": "",
        "Otro campo": "x",
    })
    assert result.state_key() == {
        "nie": "Z9999999R",
        "estado": "EN TRAMITE",
        "fecha_resolucion": "",
    }
