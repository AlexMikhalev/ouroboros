"""Round-2 card and publication regressions on the production Skills page."""
from __future__ import annotations

import pytest

from tests._skills_page_harness import (
    URL, capture, fixture, no_overflow, open_skills, skills_browser as skills_browser,
)

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
MATRIX = [(engine, width, theme) for engine in ("chromium", "webkit")
          for width in (1280, 390) for theme in ("light", "dark")]


def _open(browser, engine, width, theme):
    page = browser.new_page(viewport={"width": width, "height": 900}, has_touch=width == 390)
    page.set_default_timeout(60_000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("window.prompt = window.confirm = window.alert = () => { throw Error('native dialog used'); };")
    if engine == "webkit" and width == 1280:
        page.add_init_script("window.pywebview = {api: {open_external_url: async () => true}};")
    elif engine == "webkit" and width == 390:
        page.add_init_script("window.Telegram = {WebApp: {platform: 'ios', ready() {}, expand() {}}};")
    rows = fixture("extensions_fix_r2.json")["skills"] + fixture("extensions_fix_r1.json")["skills"]
    sources = fixture("skill_sources.json")
    for source in sources["sources"]:
        source["confirmed"] = True
        if source["id"] == "ouroboros":
            source["trust"] = "review"
    catalog = fixture("skill_catalog.json")
    template = catalog["items"][0]
    for row in rows:
        row["submit_hub"] = {"visible": True, "task_start_allowed": True, "publication_ready": True, "state": "ready"}
        catalog["items"].append({**template, "id": f"ouroboros/{row['name']}", "name": row["name"],
                                 "source_id": "ouroboros", "source_name": "Ouroboros", "recommended": False})
    skills = open_skills(page, extensions=rows, sources=sources, catalog=catalog)
    page.goto(URL, wait_until="networkidle", timeout=120_000)
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    page.locator('.skills-card[data-skill="rejected_newer"]').wait_for()
    return page, skills, errors


def _card(page, name):
    page.locator("#skills-installed-search").fill(name)
    card = page.locator(f'.skills-card[data-skill="{name}"]')
    card.scroll_into_view_if_needed()
    return card


def _contained(page):
    assert no_overflow(page)
    assert page.locator(".marketplace-modal-body").evaluate_all(
        "nodes => nodes.every(el => el.scrollWidth <= el.clientWidth + 1)")


@pytest.mark.parametrize("engine,width,theme", MATRIX)
def test_rejected_updates_keep_reviewed_retry_and_current_candidate(skills_browser, engine, width, theme):
    browser = getattr(skills_browser, engine).launch(headless=True)
    skills = None
    try:
        page, skills, errors = _open(browser, engine, width, theme)
        cases = [("rejected_newer", "its review found problems", True, False),
                 ("review_unfinished", "its review didn't finish", False, True),
                 ("landing_failed", "it couldn't be installed", False, True),
                 ("preflight_failed", "its file checks failed", True, True)]
        for name, cause, findings, skip in cases:
            card = _card(page, name)
            tray = card.locator("[data-skill-trays]")
            assert f"{cause}; the previous version runs." in tray.inner_text()
            assert card.locator(".skills-toggle").is_checked()
            assert tray.get_by_role("button", name="Update", exact=True).is_visible()
            assert tray.get_by_role("button", name="See findings", exact=True).count() == int(findings)
            assert tray.get_by_role("button", name="Update without review", exact=True).count() == int(skip)
            if name == "rejected_newer":
                assert "v0.3.3 is available." in tray.inner_text()
            _contained(page)
            capture(page, engine, width, f"r2-{theme}-{name}")
            if skip:
                tray.get_by_role("button", name="Update without review", exact=True).click()
                dialog = page.locator(".confirm-dialog")
                assert cause in dialog.inner_text()
                dialog.locator("[data-confirm-cancel]").last.click()
                assert skills.posted(f"/api/skills/{name}/update") == []

        newer = _card(page, "rejected_newer")
        newer.get_by_role("button", name="Update", exact=True).click()
        skills.wait_for_count(("POST", "/api/skills/rejected_newer/update"), 1, timeout_ms=60_000)
        row = fixture("extensions_fix_r2.json")["skills"][0]
        expected = {"mode": "replace", "digest": row["install"]["update_available"]["digest"], "local_digest": row["content_hash"]}
        assert skills.posted("/api/skills/rejected_newer/update") == [expected]

        page.locator('[data-tab="browse"]').click()
        page.locator('[data-browse-search]').fill("rejected_newer")
        browse = page.locator('[data-browse-board] .marketplace-card[data-item="ouroboros/rejected_newer"]').first
        assert "v0.3.3 available" in browse.inner_text()
        assert browse.get_by_role("button", name="Open in Installed", exact=True).count() == 0
        browse.get_by_role("button", name="Update", exact=True).click()
        skills.wait_for_count(("POST", "/api/skills/rejected_newer/update"), 2, timeout_ms=60_000)
        assert skills.posted("/api/skills/rejected_newer/update") == [expected, expected]
        _contained(page)
        assert errors == []
    finally:
        if skills:
            skills.close(browser)
        else:
            browser.close()
