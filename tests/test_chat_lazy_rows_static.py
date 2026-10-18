"""Lazy rows (owner 18.10, 5A): while a feed's width moves, the browser lays out only its rows near the screen; at
rest every row is laid out, so history paging, landings and the scroll position stay exact.

A window-resize step on the owner's Main (150 rows, WebKit 1108x680) lays out in ~28ms with lazy rows instead of
~49ms. A rule that leaves rows lazy at rest moved the passage being read by hundreds of pixels per scroll step in
WebKit (a row takes its real size only as it nears the screen) and left Chromium short of the newest message, so the
lazy rule exists only under [data-resizing] and the reading owner removes the mark once the width holds,
re-applying the place in that frame. tests/test_chat_lazy_rows_browser.py proves the behaviour in both engines.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The feeds and the rows they hold: a rule naming any of them may not make a row lazy at rest.
FEEDS = ("#chat-messages", ".chat-messages", ".chat-bubble", ".chat-live-card")


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _rules(css: str):
    """(selector, body) of every innermost rule, comments stripped."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(" ".join(match.group(1).split()), match.group(2))
            for match in re.finditer(r"([^{}@;]+)\{([^{}]*)\}", css)]


def test_feed_rows_remember_their_size_and_are_lazy_only_while_the_width_moves():
    rules = _rules(_read("web/style.css"))
    rows = [body for selector, body in rules if selector == "#chat-messages > *, .chat-messages > *"]
    assert any(re.search(r"contain-intrinsic-size:\s*auto\s+\d+px\s*;", body) for body in rows), (
        "every row remembers the size it was last laid out at, so a lazy row keeps it exactly")
    assert (":is(#chat-messages, .chat-messages)[data-resizing] > *", "content-visibility: auto;") in [
        (selector, " ".join(body.split())) for selector, body in rules], "the feeds' lazy-row rule"
    # No other rule makes a feed's rows lazy, in any stylesheet: lazy at rest moves the passage being read.
    for path in sorted((REPO / "web").glob("*.css")):
        for selector, body in _rules(path.read_text(encoding="utf-8")):
            if not re.search(r"content-visibility\s*:", body) or not any(feed in selector for feed in FEEDS):
                continue
            assert selector == ":is(#chat-messages, .chat-messages)[data-resizing] > *", (
                f"{path.name} {selector}: a feed's rows are lazy only while its width moves")


def test_the_reading_owner_marks_a_width_move_and_reapplies_the_place_once_it_holds():
    js = _read("web/modules/chat_reading_position.js")
    note = js.split("const noteWidth = () => {", 1)[1].split("\n    };", 1)[0]
    assert "toggleAttribute?.('data-resizing', true)" in note, "a width move marks the feed"
    assert "now && rowsWidth" in note, "a feed shown again (no width before) is not a move"
    held = js.split("const holdLazyRows = () =>", 1)[1].split("\n    });", 1)[0]
    unmark = held.index("toggleAttribute?.('data-resizing', false)")
    assert held.index("state.reflow()", unmark) > unmark, "the place is re-applied after every row is laid out again"
    reflow = js.split("        reflow() {", 1)[1].split("\n        },", 1)[0]
    assert reflow.lstrip().startswith("noteWidth();"), "every reflow (the feed's resize) notes the width first"
