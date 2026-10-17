"""Design-system debt ratchets for first-party sheets (P10/P11)."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def first_party_sheets():
    return sorted(path for path in WEB.rglob("*.css") if "vendor" not in path.relative_to(WEB).parts)


def without_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


# No exception: status/provenance/stat labels keep the renderer's sentence case.
UPPERCASE_ALLOWLIST = {}


def test_first_party_styles_do_not_uppercase_labels():
    violations = []
    for path in first_party_sheets():
        css = without_comments(path.read_text(encoding="utf-8"))
        if re.search(r"text-transform\s*:\s*uppercase\b", css, re.I):
            violations.append(str(path.relative_to(ROOT)))
    assert not violations, f"Sentence case is a design-system rule: {violations}"
    assert not UPPERCASE_ALLOWLIST
