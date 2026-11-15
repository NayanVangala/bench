# Netproof 3D view

The 3D layer renders a circuit as an interactive Three.js scene: components
as 3D meshes on a ground plane, wires between nodes, and live simulated
values shown as overlays on the parts they belong to.

## Status

All seven modules in `web/3d/` exist and are wired into `web/index.html`:
the page carries a `#scene3d` container, an import map mapping `three`
to the unpkg 0.160.0 URL, and a guarded dynamic import of
`./3d/main3d.js` (in `try/catch`: if the import or init fails, the flat
page keeps working and the 3D container shows the failure). The server
serves the whole `web/` directory same-origin, so the import resolves
instead of 404ing.

Test coverage is thin and uneven, and nothing here is browser-verified.
Only `main3d.js` has an HTTP serving test
(`test_server.py::test_3d_main_script_served_as_javascript` GETs
`/3d/main3d.js` and checks for a 200, a JavaScript content type, and
`init3D` in the body). The other six modules are covered only by
`backend/tests/test_3d.py`, which checks on disk that each file exists,
is non-empty, and contains its key function name as a string — no JS is
executed and no module is imported. Live in-browser rendering is not
covered by the backend test suite.

## What the 3D layer is (and is not)

- **Presentation and editing only.** It draws the circuit and lets you move
  parts and edit values. It never verifies anything.
- **Verification stays on the backend: ngspice + `rules.py`.** A verdict
  only changes when the server runs a DC operating-point simulation with
  ngspice and checks the result with `rules.check_rules` / `rules.verify`
  (`backend/netproof/rules.py`). The 3D code contains no SPICE, no rule
  thresholds, and no verdict logic of its own.
- Editing a value in 3D edits the netlist draft. No edit auto-posts:
  saving only merges the edit and patches the page's table rows, and
  the new verdict appears only after the edited netlist is POSTed to
  `/check` (manual Re-check button) and the server returns a fresh
  `Report`. Dragging a component only moves its mesh; it
  never changes the netlist, the simulation, or the verdict.

## Dependency: Three.js via CDN, frontend only

- **Three.js `0.160.0`, loaded from a CDN. No backend dependency is
  added** (`pyproject.toml` is untouched), no npm install, no build step.
  The MVP page (`web/index.html`) is otherwise CDN-free; the 3D view is
  the one CDN exception.
- Pin the version exactly. Do not float it (`three@0.160.0`, not
  `three@latest`). Every module in `web/3d/` imports Three.js by the
  bare specifier `'three'` (and `OrbitControls` by
  `'three/addons/controls/OrbitControls.js'`), resolved by the host
  page's import map — no module in this directory imports Three.js
  from a direct CDN URL:

```js
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
```

The import map lives in `web/index.html` and maps `'three'` (and
`'three/addons/'`) to the unpkg 0.160.0 URLs. `OrbitControls` itself
imports the bare specifier `'three'`, so that map must be present
before any module in this layer can run in a browser, and because
every import resolves to the same URL there is exactly one THREE
instance in the page. A direct-URL import anywhere would load a
second instance and break instanceof / material sharing.
- If the CDN is unreachable, the 3D view shows an error and does nothing
  else. The 2D netlist table in `web/index.html` keeps working; the 3D
  layer must never block `/check`, `/repair`, or downloads.

## Controls

| Input | Action | Status |
| --- | --- | --- |
| Left-drag on empty space | Orbit (rotate the camera around the circuit) | Implemented: `OrbitControls` in `scene.js`; `interaction.js` returns early when the press hit no component, so the drag reaches the controls |
| Scroll wheel | Zoom in / out | Implemented in `scene.js` (`OrbitControls`) |
| Right-drag | Pan across the ground plane | Implemented in `scene.js` (`OrbitControls`) |
| Left-drag on a component | Move that component's mesh on the ground plane (layout only, see above) | Implemented in `interaction.js` (`setupInteraction`, `onMove`); the mesh moves on y = 0 and its node anchors / wire hooks shift with it. `interaction.js` disables `OrbitControls` while a component drag is in progress, so the drag does not also orbit |
| Click a component | Select it (highlight + show its values in the side panel) | Implemented in `interaction.js` (raycast pick, `onSelect` callback; only fires when the pointer moved less than `CLICK_THRESHOLD_PX = 6`, so an orbit drag never selects by accident). `scene.js` has its own separate 5 px `pickAt` on pointer-up that only scales the picked mesh in place; `main3d.js` calls `createScene(element)` with no `onPick`, so that `onPick` callback is never wired and does not notify the page |
| Double-click a component | Edit it (id, type, value, nodes) in place, then re-check | Implemented as far as the panel: `interaction.js` fires `onEdit` on double-click, `main3d.js` opens the edit panel (`edit-panel.js`). Saving only merges the edit into the working netlist and patches the page's table rows (`applyEdit` in `web/index.html`, no fetch); it does not re-check. The panel's Re-check / Repair buttons are disabled in this wiring because the page supplies no `onRecheck` / `onRepair` hooks. The only re-check is the manual Re-check button in the page, which POSTs the edited table to `/check` |

Click-select and drag-a-component are distinguished by movement: a
press-and-release that moved less than 6 px selects; a press that moves
past 6 px on a component drags that component instead of orbiting.
Pressing empty space and moving orbits, never drags a component.

## Modules (`web/3d/*.js`)

All seven modules below exist. There is no `main.js`, `layout.js`,
`colors.js`, or `api.js` in `web/3d/` — layout lives in
`components.js` (`layoutNodes`, `buildCircuit`) plus `scene.js`'s
x-axis `SPACING = 2.2` for its own `addComponent` path, colours live
in three separate tables (see "Verdict colors" below), and there is no
fetch-adapter module: the only fetches are in `web/index.html`, which
POSTs `/check`, `/repair`, `/run-image`, and `/run-goal` and passes
the resulting `Report` into `main3d.js`.

| Module | Does |
| --- | --- |
| `main3d.js` | **Exists.** Entry point: `init3D(container, callbacks, options)` (also the default export; exports also `normalizeNetlist`, `normalizeReport`, `normalizeHistory`). Builds the scene via `scene.js`, builds the circuit via `components.js`, wires `interaction.js` (`onSelect` / `onMove` / `onEdit`), wires `overlay.js` (labels + flow dots, driven from the frame loop), opens `edit-panel.js` on double-click, and renders the repair history via `history.js`. Owns no geometry, layout, physics, or electrical numbers: every value it shows comes from its arguments. `onMove` is layout-only and never calls `onNetlistChange`; `onRecheck` / `onRepair` are only passed to the edit panel when the page supplied them (the page does not, so those buttons are disabled). Calls `createScene(element)` with no `onPick`, so `scene.js`'s `onPick` is never wired. |
| `scene.js` | **Exists.** `createScene(container, {onPick})`, `addComponent` / `addComponents` / `removeComponent` / `clearComponents`, `getComponentMesh`, and `updateVerdictColors` / `setComponentVerdict` / `resetVerdictColors`. Builds renderer, camera, lights, ground grid, and `OrbitControls`; its own pointer-up `pickAt` (5 px threshold) scales the picked mesh; one mesh builder per component type (`resistor`, `capacitor`, `led`, `diode`, `voltage_source`, `current_source`, generic box for unknown types); auto-layout spacing components along the x axis (`SPACING = 2.2` scene units) for its own `addComponent` path. Holds the verdict-color table (`VERDICT_COLORS`) and per-type default body colors (`COMPONENT_COLORS`). No simulation numbers live here: colors come only from verdict values the caller passes in, which must come from a real `/check` run. |
| `components.js` | **Exists.** `buildCircuit(netlist)` -> `{ group, nodePositions, meshesById: Map }`, plus `buildComponentMesh`, `layoutNodes`, `normalizeNetlist`, `ledColorHex`, `resistorBandColors`. Layout is a node grid by BFS hop distance from ground (`GRID_X = 3`, `GRID_Y = 2` scene units), with wires inside each component group. Throws on rows without an id or without exactly two distinct nodes, so `main3d.js` pre-filters those rows out. |
| `interaction.js` | **Exists.** `setupInteraction(scene, camera, renderer, meshesById, callbacks)` -> `{ dispose, select, deselect, getSelectedId, setMeshes, setControls, refreshWires }`, with callbacks `onSelect(componentId \| null)`, `onMove(componentId, {x, y, z})`, `onEdit(componentId)` (double-click), plus optional `onDragStart` / `onDragEnd`. Click-select at `CLICK_THRESHOLD_PX = 6`; dragging a component moves its mesh on the ground plane (y = 0) and shifts node anchors / wire hooks; pressing empty space never drags. Never fetches, never POSTs, never calls an LLM. |
| `overlay.js` | **Exists.** `initOverlays`, `updateOverlays(report, meshesById, extra?)`, `animateFlow`, `updateLabelPositions`, `setWires`, `severityForComponent`, `tintMesh`, `disposeOverlays`. Live-number labels on meshes/nodes (see "Live numbers" below) plus current-flow dots. Renders values only; it never computes any. Holds its own colour table (`SEVERITY_COLORS`, see "Verdict colors"). |
| `edit-panel.js` | **Exists.** `showEditPanel(component, report, {onSave, onRecheck, onRepair, onClose})` / `hideEditPanel`. Plain JS, no build step, no CDN, no fetch, no LLM calls. Edit form for the selected component (id, type, value, nodes, plus LED color); shows the component's measured values from the passed-in `Report` and falls back to `—` when a value is absent, never a guess. It never POSTs anything: `onSave` / `onRecheck` / `onRepair` hand the edited component to the caller, which updates the netlist and POSTs `/check` (buttons without a callback are disabled). |
| `history.js` | **Exists.** UMD, not an ES module: no exports; importing it is a side effect that sets `window.NetproofHistory`. `NetproofHistory.renderHistory(history, { onShowRound }, mount?)` -> `{ element, group, select, getSelectedIndex, getEntries, attachScene, detachScene, update, destroy }`. Repair-history timeline; viewing a round is display-only in `main3d.js`. Holds its own verdict-color table using the 2D banner hexes (see "Verdict colors"). |

## Live numbers: ngspice -> Report -> overlay, never invented

The only numbers the 3D view ever shows come from a server `Report`.
The flow is one way, backend to frontend:

```
netlist --(ngspice, DC operating point)--> SimResult
  {node_voltages: {node: volts}, branch_currents: {refdes: amps}}
    --(rules.verify / check_rules)--> Report
  {verdict, findings, node_voltages, branch_currents,
   power, netlist_used, uncertain}
    --(JSON, POST /check or /run-goal or /run-image, fetched by
       web/index.html, passed to main3d.js updateReport)--> overlay.js
```

There is no `api.js` in `web/3d/`: no module in this directory fetches
anything. The fetches live in `web/index.html` only, and the 3D layer
receives the `Report` as an argument from the page.

Field mapping (from the `ir.Report` in `backend/netproof/ir.py`,
as serialised by `model_dump` on the `/check` path):

| Report field | Shown as | Where |
| --- | --- | --- |
| `node_voltages[node]` | Node voltage, volts | Label at the node |
| `branch_currents[refdes]` | Branch current, amps | Label on the component |
| `power[refdes]` | Dissipated power, watts | Label on the component |
| `findings[].message` | Rule text (already states the measured number and the limit, e.g. `"R1 dissipates 0.50 W, over its 0.25 W rating"`) | Findings list / selected-component panel |
| `uncertain[]` | Checks that could not run | Shown verbatim; a PASS with `uncertain` entries is never presented as fully verified |

**Power field name:** there is no second `Report` shape and no
`Report.to_dict()` in `rules.py` emitting a different power key. The
only `Report` model is `ir.Report`, whose power field is `power`
(`rules._component_power` computes it; `rules.verify` returns an
`ir.Report`). `component_power` appears nowhere in the backend as a
`Report` field; `web/index.html`, `main3d.js`, and `overlay.js` only
tolerate it as a fallback alias (`report.power ||
report.component_power || report.per_component_power`), so a report
that carried only `component_power` would still read, but no server
path produces one.

Rules for the overlay:

- **Never invent a number.** If a value is absent from the `Report`
  (no sim yet, sim failed, or the check was underdetermined), the label
  shows `—`, not `0` and not a browser-side estimate.
- **No physics in the browser.** No Ohm's law, no KCL, no power math in
  any `web/3d/*.js` file. Component power comes from
  `Report.power` (`component_power` is only a tolerated alias, see
  above), which `rules.py` derives from the simulated voltages and currents
  (resistor `V^2/R` or `I^2*R`, LED `|V_anode - V_cathode| * |I_led|`,
  source `|V+ - V-| * |I|`). The one arithmetic exception is display-only:
  a component's voltage is shown as `V(n1) - V(n2)` from the Report's
  node voltages, because the Report carries node voltages, not
  per-component voltages (`edit-panel.js` does this).
- **Before the first check, the scene shows no numbers at all** — meshes
  and labels for values are empty, not zeroed.
- Node `"0"` is ground and is always `0 V` when present in the results.

## Verdict colors

There is no single colour table and no `colors.js`: three modules
each carry their own, and they disagree, so do not claim the views
stay in sync through one place.

The 2D verdict banner (`#verdict` in `web/index.html`) uses PASS green
`#1a7f37`, WARN amber `#92400E`, STOP red `#b00020`.

`scene.js` `VERDICT_COLORS` (numeric hex, the mesh edge/tint colours —
the 3D render is flat blueprint white/pale-blue, no emissive, no dark scene):

| Verdict / severity | `scene.js` |
| --- | --- |
| `PASS` | `#1a7f37` |
| `WARN` | `#92400E` |
| `STOP` | `#d92d20` |
| `INFO` | `#667085` |
| `UNKNOWN` (no verdict yet) | `#0B3D91` (blueprint blue: no glow, no signal) |

`overlay.js` `SEVERITY_COLORS` (the table on the live tinting path —
`main3d.js` tints through `updateOverlays`, not through
`scene.js`'s `updateVerdictColors`, which no live caller invokes):

| Verdict / severity | `overlay.js` |
| --- | --- |
| `PASS` | `#1a7f37` (the 2D green, not brightened) |
| `WARN` | `#92400E` |
| `STOP` | `#d92d20` (same as `scene.js`) |
| `INFO` | `#667085` (grey; never used for tinting — INFO findings never colour a part) |
| `UNKNOWN` | `#4B5563` |

`history.js` has a third, internal table using the 2D banner hexes
(`PASS #1a7f37`, `WARN #92400E`, `STOP #b00020`, unknown `#4B5563`).

STOP is split by surface: `#b00020` is the canonical STOP for text and
borders (2D banner, `history.js`); `#d92d20` is only the 3D mesh
edge/tint red in `scene.js` `VERDICT_COLORS` and `overlay.js`
`SEVERITY_COLORS`. Do not use `#d92d20` for text or borders, and do not
use `#b00020` for a mesh edge or tint.

Do not invent new verdict hues, and do not change one table expecting
the others to follow: any colour change has to be made in each table
that uses it.

- The overall `Report.verdict` (any STOP finding -> `STOP`, else any WARN
  finding -> `WARN`, else `PASS`; derived in `Report`, never recomputed in
  JS) drives the scene-level verdict indication. On the live overlay
  path, a component with no findings is tinted PASS green only when
  the overall verdict is PASS and no severe finding that names no
  component (e.g. `NO_GROUND`) blocks that claim; otherwise it is
  UNKNOWN grey (`severityForComponent` in `overlay.js`). It does not
  simply keep its neutral per-type body color (`COMPONENT_COLORS` in
  `scene.js`, used only for the initial mesh build).
- Per-component highlight comes from the findings: `scene.js`
  `updateVerdictColors` accepts a per-component map
  (`{R1: 'PASS', D1: 'STOP'}`), one verdict plus a list of ids, or a
  findings array, and tints each named mesh — STOP red, WARN amber.
  The caller derives that map from `findings[].component_ids`
  (the `ir.Finding` shape; there is no separate `rules.py` dataclass
  finding shape). Severity order is STOP first, then WARN, then INFO.
- A selected component gets an outline/highlight distinct from verdict
  colors, so selection is never confused with a STOP (`interaction.js`
  draws a blue `BoxHelper` outline plus an emissive highlight;
  `scene.js` scales the selected mesh to 1.12x).

## Rules the backend checks (the 3D view displays these, it does not run them)

| Rule | Meaning |
| --- | --- |
| `NO_GROUND` | No ground (reference node) in the circuit |
| `FLOATING_NODE` | A node is not connected to anything that defines its voltage |
| `SHORTED_SOURCE` | A voltage source is shorted out |
| `SHORTED_COMPONENT` | A component is bypassed by a short |
| `NO_SOURCE` | No power source in the circuit |
| `LED_OVERCURRENT` | LED current exceeds its safe limit (STOP over 30 mA, WARN over 20 mA) |
| `LED_UNDERCURRENT` | LED current below 1 mA, too low to light usefully |
| `REVERSED_LED` | An LED is wired backwards (reverse-biased) |
| `RESISTOR_POWER` | A resistor dissipates more power than its rating (default 0.25 W) |
| `SOURCE_OVERCURRENT` | A source is asked to supply more current than it can (over 1 A) |

Thresholds live in `backend/netproof/parts.py` (LED limits) and
`backend/netproof/rules.py` (all rule thresholds). If a threshold changes,
only the backend changes; the 3D layer picks the new verdict up through
the next `Report`.

## Limits (inherited from the MVP)

- **DC operating point only.** ngspice runs a DC op simulation; no
  transient or AC analysis, so nothing time-, switching-, or
  frequency-dependent (capacitor charging, oscillation, filters, PWM) is
  checked, in 2D or 3D.
- The 3D view is only as good as the netlist and models supplied. Wrong
  values in, wrong verdict out.
- No accuracy claims: the guarantee is only that every verdict is derived
  from a real simulation run against the rule list above, never from the
  language model alone. The LLM may propose circuits, explanations, and
  repairs; it never decides a verdict.
