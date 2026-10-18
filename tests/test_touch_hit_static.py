"""One touch hit-extension recipe (docs/DESIGN.md "Shell"; plan P10): a small control keeps its look and gains an
invisible centred hit area of the touch minimum under a coarse pointer. The recipe lives once in web/ui.css
(`.ui-hit-area`); the composer's and the feed's small controls carry the class; no sheet repeats the formula."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORMULA = "inset: min(0px, calc((100% - var(--touch-target-min)) / 2));"
CARRIERS = {
    "web/modules/chat.js": ('class="chat-swarm ui-hit-area"', 'class="chat-attach-btn ui-hit-area"', 'class="chat-send-inline ui-hit-area"'),
    "web/modules/chat_render_batch.js": ("button.className = 'chat-load-older-btn ui-hit-area';",),
    "web/modules/composer_owner_controls.js": ('class="chat-capsule-head ui-hit-area ${headClass}"',),
}


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_the_hit_extension_recipe_lives_once_in_ui_css_and_the_small_controls_carry_it():
    ui = _read("web/ui.css")
    assert ui.count(FORMULA) == 1
    base = ui.split("\n.ui-hit-area,", 1)[1].split("}", 1)[0]
    assert "position: relative" in base
    coarse = ui.split("\n.ui-hit-area,", 1)[1].split("@media (pointer: coarse) {", 1)[1].split("}\n}", 1)[0]
    assert ".ui-hit-area::after" in coarse
    assert 'content: ""' in coarse and "position: absolute" in coarse and FORMULA in coarse
    # A page's links take the same recipe instead of a 44px box, so a link inside a sentence keeps its lines.
    assert "a[href]::after" in coarse and "a[href]" in base
    page_links = _read("web/style.css").split(".settings-shell) a[href] {", 1)[1].split("}", 1)[0]
    assert "inline-flex" not in page_links and "min-block-size" not in page_links
    for rel in ("web/style.css", "web/settings.css", "web/room_work.css", "web/onboarding.css"):
        assert FORMULA not in _read(rel), f"{rel} repeats the recipe"
    for rel, markers in CARRIERS.items():
        source = _read(rel)
        for marker in markers:
            assert marker in source, f"{rel} lacks {marker}"
    # The composer rows part by --space-2 (8px): a 32px control's 6px overshoot never meets its neighbour's.
    css = _read("web/style.css")
    wrap = css.split(".chat-input-wrap {", 1)[1].split("}", 1)[0]
    assert "gap: var(--space-2)" in wrap
