from bs4 import BeautifulSoup


def extract_result_fields(html: str) -> dict[str, str]:
    """Each result field is rendered as <label>Name:</label><div class="dvResB">value</div>."""
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    for label in soup.find_all("label"):
        text = label.get_text(strip=True)
        if text.endswith(":"):
            key = text[:-1].strip()
            value_div = label.find_next_sibling("div")
            fields[key] = value_div.get_text(strip=True) if value_div else ""
    return fields
