import test from 'node:test';
import assert from 'node:assert/strict';
import { renderPageHeader } from '../modules/page_header.js';

test('page headers retain their toolbar contract with an optional chrome recipe', () => {
    for (const field of ['toolbarHtml', 'actionsHtml']) {
        const basic = renderPageHeader({ title: 'Chat', [field]: '<button>Act</button>' });
        assert.match(basic, /class="app-page-toolbar app-page-actions"/);
        const grouped = renderPageHeader({ title: 'Chat', [field]: '<button>Act</button>', toolbarClass: 'ui-chrome-group' });
        assert.match(grouped, /class="app-page-toolbar app-page-actions ui-chrome-group"/);
        assert.equal((grouped.match(/<button>Act<\/button>/g) || []).length, 1);
    }
    assert.doesNotMatch(renderPageHeader({ title: 'Chat', toolbarClass: 'ui-chrome-group' }), /app-page-toolbar/);
});

test('one header: no icon, the lede and the tabs are siblings after the row, a Project keeps its name', () => {
    const page = renderPageHeader({ title: 'Widgets', icon: '<svg/>', description: 'Reviewed surfaces live here.' });
    assert.doesNotMatch(page, /app-page-icon|<svg\/>/, 'no page icon anywhere (owner 5A); the ☰ glyph is a control');
    assert.match(page, /<\/div>\s*<p class="app-page-lede">Reviewed surfaces live here\.<\/p>/, 'the description follows the header');
    assert.doesNotMatch(page, /app-page-description/);
    assert.match(page, /<div class="page-header app-page-header" data-sidebar-names data-empty>/, 'named by the sidebar, nothing else to say');
    const tabbed = renderPageHeader({ title: 'Dashboard', tabsHtml: '<div role="tablist"></div>' });
    assert.match(tabbed, /<\/div>\s*<div class="app-page-tabs"><div role="tablist"><\/div><\/div>/, 'tabs are their own strip');
    assert.equal(tabbed.indexOf('app-page-tabs') > tabbed.indexOf('</div>'), true);
    assert.match(tabbed, /data-sidebar-names data-empty>/, 'tabs alone leave the row empty on a desk');
    const acting = renderPageHeader({ title: 'Files', actionsHtml: '<button>Refresh</button>' });
    assert.match(acting, /data-sidebar-names>/);
    assert.doesNotMatch(acting, /data-empty/);
    const project = renderPageHeader({ title: '', titleId: 'project-panel-title', named: false, trailingHtml: '<button>×</button>' });
    assert.match(project, /<h2 class="app-page-title" id="project-panel-title"><\/h2>/);
    assert.doesNotMatch(project, /data-sidebar-names|data-empty/, 'a Project is named by its owner: its name stays');
    assert.match(project, /app-page-trailing/);
    assert.match(project, /data-mobile-nav-toggle/, '☰ is in every header, the Project included');
    assert.match(renderPageHeader({ title: 'x', titleId: '"><i>' }), /id="&quot;&gt;&lt;i&gt;"/);
    assert.doesNotMatch(renderPageHeader({ title: 'x', showMobileNav: false }), /data-mobile-nav-toggle/);
});

test('the optional toolbar class cannot create an HTML attribute or node', () => {
    const html = renderPageHeader({ title: 'Chat', actionsHtml: '<button>Act</button>', toolbarClass: '\" onclick=\"alert(1)\"><img src=x>' });
    assert.doesNotMatch(html, /class="app-page-toolbar app-page-actions " onclick=/);
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&quot;/);
    assert.match(html, /&lt;img/);
});
