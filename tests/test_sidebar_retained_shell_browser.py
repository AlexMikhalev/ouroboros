"""A retained pre-three-zone sidebar must still accept a real mouse wheel.

Uses the actual SPA and API fixture, not a substitute navigation component.
"""
import pytest
from tests.test_design_system_gate_browser import seed_page, settle, WEB
from tests.test_subscription_setup_browser import subscription_ui  # noqa: F401

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.parametrize('height', [928, 700, 500])
@pytest.mark.parametrize('retained', [False, True])
def test_sidebar_mouse_wheel_reaches_projects(subscription_ui, height, retained):
    ui = subscription_ui
    page = seed_page(ui, 1280, height, touch=False)
    if retained:
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        start = html.index('>', html.index('<nav id="primary-sidebar"')) + 1
        end = html.index('</nav>', start)
        html = html[:start] + '<div class="sidebar-scroll">' + html[start:end] + '</div>' + html[end:]
        page.route(ui['url'] + '/', lambda route: route.fulfill(content_type='text/html', body=html))
    try:
        page.goto(ui['url'] + '/')
        page.wait_for_selector('#nav-projects-list [data-project-id]')
        settle(page)
        position = """() => [...document.querySelectorAll('#primary-sidebar, .sidebar-scroll, #nav-projects-list')]
            .reduce((sum,e)=>sum+e.scrollTop,0)"""
        before = page.evaluate(position)
        box = page.locator('#nav-projects-list').bounding_box()
        page.mouse.move(box['x'] + box['width']/2, min(box['y'] + box['height']/2, height - 40))
        page.mouse.wheel(0, 600)
        page.wait_for_function(f'({position})() > {before}')
        assert page.locator('.nav-project-row').first.evaluate('e=>getComputedStyle(e).fontSize') == '14px'
        # The list can return to Main, and the tools remain reachable too.
        page.mouse.wheel(0, -3000)
        page.wait_for_function(f'({position})() === 0')
        assert page.locator('[data-nav-page="chat"]').is_visible()
        page.locator('[data-nav-page="settings"]').scroll_into_view_if_needed()
        assert page.locator('[data-nav-page="settings"]').is_visible()
    finally:
        page.close()
