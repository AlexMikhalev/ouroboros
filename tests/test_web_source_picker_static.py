"""Model-assigning editors share one source-group primitive."""

from __future__ import annotations

import pathlib
import re


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB_MODULES = REPO_ROOT / "web" / "modules"
PRIMITIVES = WEB_MODULES / "route_editor_primitives.js"


def _import_specifiers(source: str, symbol: str) -> set[str]:
    """Module specifiers from which ``source`` imports ``symbol``.

    Handles the three real shapes in ``web/modules``: a named import, an
    aliased named import (``routeChoiceGroups as x``) and a namespace import
    whose object is then read as ``ns.routeChoiceGroups``.
    """
    specifiers: set[str] = set()
    named = re.compile(r"import\s*\{(?P<names>[^}]*)\}\s*from\s*['\"](?P<from>[^'\"]+)['\"]", re.DOTALL)
    for match in named.finditer(source):
        names = [part.split(" as ")[0].strip() for part in match.group("names").split(",")]
        if symbol in names:
            specifiers.add(match.group("from"))
    namespace = re.compile(r"import\s*\*\s*as\s+(?P<alias>\w+)\s*from\s*['\"](?P<from>[^'\"]+)['\"]")
    for match in namespace.finditer(source):
        if re.search(rf"\b{re.escape(match.group('alias'))}\s*\.\s*{re.escape(symbol)}\b", source):
            specifiers.add(match.group("from"))
    return specifiers


def _reexports(source: str, symbol: str) -> bool:
    """``export * from './route_editor_primitives.js'`` or an explicit
    ``export { symbol } from`` / re-declared wrapper of the same name."""
    if re.search(r"export\s*\*\s*from\s*['\"]\./route_editor_primitives\.js['\"]", source):
        return True
    for match in re.finditer(
        r"export\s*\{(?P<names>[^}]*)\}\s*from\s*['\"](?P<from>[^'\"]+)['\"]", source, re.DOTALL
    ):
        names = [part.split(" as ")[-1].strip() for part in match.group("names").split(",")]
        if symbol in names and "route_editor_primitives" in match.group("from"):
            return True
    # A same-named wrapper that delegates to the primitive counts: the source
    # list still has ONE owner.
    if re.search(rf"export\s+function\s+{re.escape(symbol)}\b", source):
        return bool(_import_specifiers(source, symbol) or re.search(
            rf"\w+\s*\.\s*{re.escape(symbol)}\b", source))
    return False


def test_model_assigning_editors_share_the_grouped_source_select() -> None:
    """Models roles and Available subagents (whose rows marked Reviewer are the
    reviewers) take their source groups from ONE primitive, directly or through a
    module that re-exports it.

    docs/DESIGN.md §7: every model-assigning surface offers "one grouped source
    select with the same groups in the same order". Private copies of the group
    list is exactly how that order drifts apart per surface.
    """
    symbol = "routeChoiceGroups"
    editors = ("model_roles.js", "subagents_settings.js")

    primitives_source = PRIMITIVES.read_text(encoding="utf-8")
    assert re.search(rf"export\s+(?:function|const)\s+{symbol}\b", primitives_source), (
        f"web/modules/route_editor_primitives.js must export {symbol}"
    )
    assert re.search(r"export\s+(?:function|const)\s+configuredApiProviders\b", primitives_source), (
        "web/modules/route_editor_primitives.js must export configuredApiProviders — the "
        "API keys group lists one entry per provider with a stored credential (docs/DESIGN.md §7)"
    )

    missing: list[str] = []
    for name in editors:
        path = WEB_MODULES / name
        source = path.read_text(encoding="utf-8")
        specifiers = _import_specifiers(source, symbol)
        if any("route_editor_primitives" in spec for spec in specifiers):
            continue
        # One hop: an editor may take the primitive through a sibling that
        # re-exports (or thinly wraps) it rather than importing it directly.
        if any(
            (WEB_MODULES / pathlib.Path(spec).name).is_file()
            and _reexports((WEB_MODULES / pathlib.Path(spec).name).read_text(encoding="utf-8"), symbol)
            for spec in specifiers
        ):
            continue
        missing.append(f"web/modules/{name}")

    assert not missing, (
        f"these model-assigning editors do not take {symbol} from "
        "web/modules/route_editor_primitives.js (directly or via a re-export): "
        + ", ".join(missing)
    )
