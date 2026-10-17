"""The first-run wizard's Starter skills row through the production wizard.

Reuses the wizard's static-server/API-boundary fixture: the starter list is the gateway's
`GET /api/onboarding/starter-skills` answer from web/tests/fixtures/skills_v4, and the one atomic
completion carries the owner's ticks and hub trust choices. Nothing installs here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_subscription_setup_browser import capture, subscription_ui as subscription_ui

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
STARTER = json.loads((Path(__file__).resolve().parents[1] / "web/tests/fixtures/skills_v4/onboarding_starter_skills.json")
                     .read_text(encoding="utf-8"))


def _summary(ui, *, answer=STARTER, status=200, width=1360):
    page = ui["page"]
    page.set_viewport_size({"width": width, "height": 900})
    page.route("**/api/onboarding/starter-skills", lambda route: route.fulfill(
        status=status, content_type="application/json", body=json.dumps(answer)))
    page.goto(ui["url"] + "/onboarding")
    page.wait_for_selector("#quick-start-btn:not([hidden])")
    page.click("#quick-start-btn")
    page.wait_for_selector(".summary-card")
    return page


def _completion(ui):
    return [payload for path, payload in ui["posts"] if path == "/api/onboarding/complete"]


@pytest.mark.parametrize("width", [1360, 390])
def test_starter_row_opens_into_the_categorized_list_and_the_completion_carries_the_choices(subscription_ui, width):
    ui = subscription_ui
    page = _summary(ui, width=width)
    row = page.locator(".skills-starter-row")
    row.wait_for()
    page.wait_for_function("document.querySelector('.skills-starter-row').textContent.includes('ticked')")
    assert "Starter skills" in row.inner_text()
    assert "10 · 6 ticked · Change ›" in row.inner_text()
    assert ("Ticked: telegram, pdf_tools, word_tools, duckduckgo, memory-atlas, anti-slop. 4 more you can tick."
            in row.inner_text())
    capture(page, f"starter-row-collapsed-{width}")

    page.locator("[data-starter-toggle]").click()
    panel = page.locator("[data-starter-panel]")
    panel.wait_for()
    heads = [text.split("\n")[0].strip() for text in panel.locator(".skills-starter-group h4").all_inner_texts()]
    assert heads[0].startswith("Built in — comes with the app"), heads
    assert [head.rstrip("0123456789").strip() for head in heads] == [
        "Built in — comes with the app", "Messaging", "Documents", "Search & research", "About Ouroboros", "Writing & review"]
    assert panel.locator(".skills-starter-card.active").count() == 6
    assert "They install without review — you trust Ouroboros. Skills that need a key wait for it." in panel.inner_text()
    assert "Needs: OPENROUTER_API_KEY" in panel.inner_text()
    scroll = panel.locator("[data-starter-scroll]")
    assert scroll.evaluate("el => getComputedStyle(el).overflowY") == "auto"
    trust = panel.locator(".skills-starter-trust")
    assert trust.locator('[data-starter-trust="ouroboros"][data-trust="trusted"]').get_attribute("aria-pressed") == "true"
    assert trust.locator('[data-starter-trust="ouroboros-community"][data-trust="review"]').get_attribute("aria-pressed") == "true"
    capture(page, f"starter-row-open-{width}")

    page.locator('[data-starter-item="ouroboros/pdf_tools"]').uncheck()
    page.locator('[data-starter-item="ouroboros/perplexity"]').check()
    assert "10 · 6 ticked" in row.inner_text()
    trust.locator('[data-starter-trust="ouroboros-community"][data-trust="trusted"]').click()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.click("#next-btn")
    for _ in range(200):
        if _completion(ui):
            break
        page.wait_for_timeout(25)
    payload = _completion(ui)[0]
    assert payload["starter_skills"] == ["builtin/telegram", "ouroboros/word_tools", "ouroboros/duckduckgo",
                                         "ouroboros/perplexity", "ouroboros/memory-atlas", "ouroboros/anti-slop"]
    assert payload["skill_source_trust"] == {"ouroboros": "trusted", "ouroboros-community": "trusted"}


def test_untick_all_installs_nothing_and_an_unreadable_list_keeps_the_host_default(subscription_ui):
    ui = subscription_ui
    page = _summary(ui)
    page.locator("[data-starter-toggle]").click()
    page.locator('[data-starter-all="0"]').click()
    page.locator("[data-starter-toggle]").click()
    assert "Nothing is ticked: no skills install now." in page.locator(".skills-starter-row").inner_text()
    page.click("#next-btn")
    for _ in range(200):
        if _completion(ui):
            break
        page.wait_for_timeout(25)
    assert _completion(ui)[0]["starter_skills"] == []

    ui["posts"].clear()
    page = ui["new_page"]()
    ui["page"] = page
    page = _summary(ui, answer={"error": "starter list unavailable"}, status=503)
    page.wait_for_function("document.querySelector('.skills-starter-row').textContent.includes('The default set')")
    assert "Ouroboros installs the recommended skills after it starts" in page.locator(".skills-starter-row").inner_text()
    capture(page, "starter-row-unreadable")
    page.click("#next-btn")
    for _ in range(200):
        if _completion(ui):
            break
        page.wait_for_timeout(25)
    payload = _completion(ui)[0]
    assert "starter_skills" not in payload and "skill_source_trust" not in payload, "the host's default list stays"
