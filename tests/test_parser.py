from app.core.parser import extract_result_fields

# Synthetic page mirroring the portal's result markup (real pages contain
# personal data and are never committed).
SAMPLE = """
<html><body>
<div class="dvRes">
  <label>N.I.E:</label><div class="dvResB">Z9999999R</div>
  <label>Estado de Resolución:</label><div class="dvResB">EN TRAMITE</div>
  <label>Fecha de Resolución:</label><div class="dvResB"></div>
  <label>Sin dos puntos</label><div>ignored</div>
</div>
</body></html>
"""


def test_extracts_label_div_pairs():
    fields = extract_result_fields(SAMPLE)
    assert fields["N.I.E"] == "Z9999999R"
    assert fields["Estado de Resolución"] == "EN TRAMITE"
    assert fields["Fecha de Resolución"] == ""


def test_ignores_labels_without_colon():
    fields = extract_result_fields(SAMPLE)
    assert "Sin dos puntos" not in fields


def test_empty_page():
    assert extract_result_fields("<html></html>") == {}
