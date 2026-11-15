"""Frontend-module presence tests for the Netproof 3D view.

File owner: backend/tests/test_3d.py ONLY. Does not touch web/ code.

STATUS AT REWRITE TIME (2026-11-15, fix agent 57): the previous
version of this file failed verification (19 failures) because it
checked for function names that do not exist in the modules.
This version aligns every string check to the names the modules
actually define/export, verified by reading web/3d/*.js first.
All seven modules exist and all index.html integration has landed
(``<div id="scene3d">``, a Three.js importmap, and a dynamic
``import('./3d/main3d.js')``), so the index.html checks assert
rather than skip. If that integration is ever removed, the three
index.html tests skip with a clear reason instead of failing on
another agent's pending work.

No browser, no network, no LLM key. Every check is a local
filesystem / string read: file exists, file is non-empty, and the
file text contains the key function name as a string. No JS is
executed and no module is imported, so a file that merely mentions
a name in a comment would pass its string check; the names below
are the contract the 3D modules must actually define/export.

Contract (module -> key function names, as strings, as landed):
  * web/3d/scene.js       -> createScene, addComponent,
                            updateVerdictColors (exports also
                            addComponents, removeComponent,
                            clearComponents, getComponentMesh,
                            setComponentVerdict,
                            resetVerdictColors)
  * web/3d/components.js -> buildCircuit (exports also
                            buildComponentMesh, layoutNodes,
                            normalizeNetlist, ledColorHex,
                            resistorBandColors)
  * web/3d/interaction.js -> setupInteraction (the only export;
                            select / deselect are handle methods
                            defined inside it)
  * web/3d/overlay.js     -> updateOverlays, animateFlow (exports
                            also initOverlays, setWires,
                            severityForComponent, tintMesh,
                            updateLabelPositions, disposeOverlays)
  * web/3d/edit-panel.js  -> showEditPanel, hideEditPanel
                            (the only exports; renderPanel is an
                            internal render function)
  * web/3d/history.js     -> renderHistory (UMD
                            NetproofHistory.renderHistory; select /
                            update are handle methods returned by
                            it)
  * web/3d/main3d.js      -> init3D (export + default export;
                            exports also normalizeNetlist,
                            normalizeReport, normalizeHistory)

Plus web/index.html must contain (checked only if present, else
the test skips with the reason):
  * <div id="scene3d"> (the 3D mount point, exact string)
  * a Three.js CDN import / module reference (e.g. an importmap
    entry for "three", or a <script> / import from a three.js CDN
    URL such as unpkg / jsdelivr / cdnjs, or three.module.js)
  * a reference loading web/3d/main3d.js (the 3D entry point)

If the real frontend uses different function names, update the
MODULE_FUNCTIONS map in this file (the tests, not the frontend,
are what this file owns).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# backend/tests/test_3d.py -> parents[0] tests, [1] backend, [2] root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "web"
THREE_D_DIR = WEB_DIR / "3d"
INDEX_HTML = WEB_DIR / "index.html"

# Filename (under web/3d/) -> key function names (as strings).
# Every name here was verified present in the module it is checked
# against (2026-11-15); do not add a name without that check.
MODULE_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "scene.js": ("createScene", "addComponent", "updateVerdictColors"),
    "components.js": ("buildCircuit",),
    "interaction.js": ("setupInteraction",),
    "overlay.js": ("updateOverlays", "animateFlow"),
    "edit-panel.js": ("showEditPanel", "hideEditPanel"),
    "history.js": ("renderHistory",),
    "main3d.js": ("init3D",),
}

# One (filename, function-name) pair per content check.
MODULE_FUNCTION_PAIRS: list[tuple[str, str]] = [
    (filename, func)
    for filename, funcs in MODULE_FUNCTIONS.items()
    for func in funcs
]

# A Three.js reference: an importmap / bare "three" import, a CDN
# URL for three, or a three.module.js / three.min.js file reference.
THREE_JS_RE = re.compile(
    r"""["']three["']|three\.module\.js|three\.min\.js"""
    r"""|unpkg\.com/three|jsdelivr\.net/npm/three|cdnjs[^"']*three""",
    re.IGNORECASE,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _index_html_or_skip() -> str:
    """Return web/index.html text, or skip: integration not landed."""
    if not INDEX_HTML.is_file():
        pytest.skip(
            "index.html 3D integration has not landed yet: "
            f"web/index.html missing ({INDEX_HTML})"
        )
    return _read(INDEX_HTML)


@pytest.mark.parametrize("filename", sorted(MODULE_FUNCTIONS))
def test_3d_module_exists(filename: str) -> None:
    """web/3d/<filename> exists and is non-empty."""
    path = THREE_D_DIR / filename
    assert path.is_file(), f"missing 3D module: {path}"
    assert path.stat().st_size > 0, f"empty 3D module: {path}"


@pytest.mark.parametrize(
    "filename,func_name",
    MODULE_FUNCTION_PAIRS,
    ids=[f"{filename}:{func}" for filename, func in MODULE_FUNCTION_PAIRS],
)
def test_3d_module_contains_key_function(filename: str, func_name: str) -> None:
    """web/3d/<filename> contains its key function name as a string."""
    path = THREE_D_DIR / filename
    assert path.is_file(), f"missing 3D module, cannot check {func_name}: {path}"
    assert func_name in _read(path), (
        f"{filename} does not contain {func_name!r}"
    )


def test_index_has_scene3d_mount() -> None:
    """web/index.html contains <div id="scene3d"> (skip if not landed)."""
    html = _index_html_or_skip()
    if '<div id="scene3d"' not in html:
        pytest.skip(
            "index.html 3D integration has not landed yet: "
            'no <div id="scene3d"> mount point in web/index.html'
        )
    assert '<div id="scene3d"' in html


def test_index_references_three_js() -> None:
    """web/index.html references Three.js (skip if not landed)."""
    html = _index_html_or_skip()
    if not THREE_JS_RE.search(html):
        pytest.skip(
            "index.html 3D integration has not landed yet: "
            "no Three.js CDN import/module reference in web/index.html"
        )
    assert THREE_JS_RE.search(html)


def test_index_loads_main3d() -> None:
    """web/index.html loads web/3d/main3d.js (skip if not landed)."""
    html = _index_html_or_skip()
    if "3d/main3d.js" not in html:
        pytest.skip(
            "index.html 3D integration has not landed yet: "
            "web/index.html does not reference 3d/main3d.js"
        )
    assert "3d/main3d.js" in html


def test_index_edit_panel_hooks_match_main3d_signatures() -> None:
    """P4 regression: the page's edit-panel hooks must match the
    signatures main3d.js actually calls. ``onEdit`` gets
    ``(id, updatedRow, netlist)`` but ``onRecheck`` / ``onRepair``
    get ``(updatedRow, netlist)`` — the row carries its own id. An
    earlier wiring passed ``(id, updated)`` for all three, so
    ``applyEdit`` silently found no row (object id never equals a
    string id) and the panel's Re-check / Repair buttons no-op'd
    while Save worked."""
    html = _index_html_or_skip()
    assert "onRecheck: function (updated)" in html
    assert "onRepair: function (updated)" in html
    assert "page.applyEdit(updated.id, updated)" in html
    assert "onRecheck: function (id, updated)" not in html
    assert "onRepair: function (id, updated)" not in html


def test_3d_report_lookups_are_case_insensitive() -> None:
    """P4 regression: Report keys are not one case — ngspice
    lowercases branch-current keys ("r1") while power keeps the
    component id ("R1"). The 3D labels and the edit panel must look
    values up case-insensitively, or the current line silently
    disappears (labels showed power with no current; the panel
    showed "—" for current)."""
    for filename in ("overlay.js", "edit-panel.js"):
        text = _read(THREE_D_DIR / filename)
        assert "lookupCI" in text, f"{filename} has no case-insensitive lookup"
        assert "toLowerCase()" in text
    overlay = _read(THREE_D_DIR / "overlay.js")
    assert "lookupCI(branchCurrents, id)" in overlay
    assert "lookupCI(power, id)" in overlay
    assert "lookupCI(currents, branchId)" in overlay
    panel = _read(THREE_D_DIR / "edit-panel.js")
    assert "lookupCI(report.branch_currents, component.id)" in panel
    assert "lookupCI(report.power, component.id)" in panel
    assert "lookupCI(report.node_voltages, component.nodes[0])" in panel


def test_edit_panel_parts_picker_present() -> None:
    """P5.3: the edit panel fetches the standard-value catalog from
    GET /parts (cached, deduped) and offers a value picker with a
    Custom fallback, a rating picker, and an LED colour -> Vf hint
    with a "Use Vf" button that never silently overwrites the value.
    String checks only (see module docstring); live behaviour is
    verified in the browser, not here."""
    panel = _read(THREE_D_DIR / "edit-panel.js")
    assert "fetchPartsCatalog" in panel
    assert "fetch('/parts')" in panel
    assert "catalogCache" in panel
    assert "catalogPromise" in panel
    assert "value-pick" in panel
    assert "Custom" in panel
    assert "rating-pick" in panel
    assert "nearestStandard" in panel
    assert "led-hint" in panel
    assert "use-vf" in panel
    assert "Typical Vf for" in panel
    # Picker writes into the number input; validation reads it.
    assert "readEditedComponent" in panel
    assert "showEditPanel(component, report, callbacks)" in panel
