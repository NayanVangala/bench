# Netproof 3D view

The 3D layer renders a circuit as an interactive Three.js scene: components
as 3D meshes on a ground plane, wires between nodes, and live simulated
values shown as overlays on the parts they belong to.

## Status

This README is the spec for the 3D layer, kept in step with the code that
lands. Two modules already exist (written by sibling agents while this
README was being drafted): `scene.js` and `edit-panel.js` — see the module
table below for what each one actually implements. The remaining modules
are still spec-only. Nothing in this layer is wired into `web/index.html`
yet.

## What the 3D layer is (and is not)

- **Presentation and editing only.** It draws the circuit and lets you move
  parts and edit values. It never verifies anything.
- **Verification stays on the backend: ngspice + `rules.py`.** A verdict
  only changes when the server runs a DC operating-point simulation with
  ngspice and checks the result with `rules.check_rules` / `rules.verify`
  (`backend/netproof/rules.py`). The 3D code contains no SPICE, no rule
  thresholds, and no verdict logic of its own.
- Editing a value in 3D edits the netlist draft. The new verdict appears
  only after the edited netlist is POSTed to `/check` and the server
  returns a fresh `Report`. Dragging a component only moves its mesh; it
  never changes the netlist, the simulation, or the verdict.

## Dependency: Three.js via CDN, frontend only

- **Three.js `0.160.0`, loaded from a CDN. No backend dependency is
  added** (`pyproject.toml` is untouched), no npm install, no build step.
  The MVP page (`web/index.html`) is otherwise CDN-free; the 3D view is
  the one CDN exception.
- Pin the version exactly. Do not float it (`three@0.160.0`, not
  `three@latest`). `scene.js` imports it by direct URL today:

```js
import * as THREE from 'https://unpkg.com/three@0.160.0/build/three.module.js';
import { OrbitControls } from 'https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js';
```

- `OrbitControls` itself imports the bare specifier `'three'`, so the host
  page must also carry an import map that maps `'three'` to that same
  `three.module.js` URL before `scene.js` can run in a browser. That map
  belongs in `web/index.html`, which this layer must not edit; a later
  step wires it there.
- If the CDN is unreachable, the 3D view shows an error and does nothing
  else. The 2D netlist table in `web/index.html` keeps working; the 3D
  layer must never block `/check`, `/repair`, or downloads.

## Controls

| Input | Action | Status |
| --- | --- | --- |
| Left-drag on empty space | Orbit (rotate the camera around the circuit) | Implemented in `scene.js` (`OrbitControls`) |
| Scroll wheel | Zoom in / out | Implemented in `scene.js` (`OrbitControls`) |
| Right-drag | Pan across the ground plane | Implemented in `scene.js` (`OrbitControls`) |
| Drag (same as orbit) | Rotate the view | Implemented in `scene.js` (`OrbitControls`, damping on) |
| Click a component | Select it (highlight + show its values in the side panel) | Implemented in `scene.js` (raycaster pick, `onPick` callback; only fires when the pointer barely moved, so an orbit drag never selects by accident) |
| Drag a component | Move that component's mesh on the ground plane (layout only, see above) | Planned, not in `scene.js` yet |
| Double-click a component | Edit it (id, type, value, nodes) in place, then re-check | Planned wiring: the edit panel exists (`edit-panel.js`), but no double-click handler calls it yet |

Click-select and drag-a-component are distinguished by movement: a
press-and-release with no drag selects; a press that moves past a small
threshold drags the component instead of orbiting.

## Modules (`web/3d/*.js`)

| Module | Does |
| --- | --- |
| `scene.js` | **Exists.** The whole 3D scene today: `createScene(container, {onPick})`, `addComponent` / `addComponents` / `removeComponent` / `clearComponents`, `getComponentMesh`, and `updateVerdictColors` / `setComponentVerdict`. Builds renderer, camera, lights, ground grid, and `OrbitControls`; raycast click-select with the barely-moved threshold; one mesh builder per component type (`resistor`, `capacitor`, `led`, `diode`, `voltage_source`, `current_source`, generic box for unknown types); auto-layout spacing components along the x axis (`SPACING = 2.2` scene units). Holds the verdict-color table (`VERDICT_COLORS`) and per-type default body colors (`COMPONENT_COLORS`). No simulation numbers live here: colors come only from verdict values the caller passes in, which must come from a real `/check` run. |
| `edit-panel.js` | **Exists.** `showEditPanel(component, report, {onSave, onRecheck, onRepair, onClose})` / `hideEditPanel`. Plain JS, no build step, no CDN, no fetch, no LLM calls. Edit form for the selected component (id, type, value, nodes, plus LED color); shows the component's measured values from the passed-in `Report` and falls back to `—` when a value is absent, never a guess. It never POSTs anything: `onSave` / `onRecheck` / `onRepair` hand the edited component to the caller, which updates the netlist and POSTs `/check` (buttons without a callback are disabled). |
| `main.js` | Planned. Entry point. Builds the scene via `scene.js`, wires `edit-panel.js` to double-click, loads the current netlist + latest `Report`, handles resize and CDN-load failure. |
| `layout.js` | Planned (today folded into `scene.js` as x-axis spacing). Turns the netlist graph into 3D positions: nodes as points on the ground plane, components as edges between their two nodes. Pure geometry; it never reads simulation results. |
| `overlay.js` | Planned. Live-number labels on meshes/nodes (see "Live numbers" below). Renders values only; it never computes any. (Today the only live numbers shown are in `edit-panel.js`, not on the 3D meshes.) |
| `colors.js` | Planned (today folded into `scene.js` as `VERDICT_COLORS`). The single place that maps `PASS` / `WARN` / `STOP` and finding severity to hex colors, so 3D and the 2D page can never disagree. |
| `api.js` | Planned. Small fetch adapters for the same-origin endpoints (`POST /check`, `POST /run-goal`, `POST /run-image`): same JSON shapes the 2D page uses, plus the `Report` adapter (see below). No LLM calls from the browser. |

## Live numbers: ngspice -> Report -> overlay, never invented

The only numbers the 3D view ever shows come from a server `Report`.
The flow is one way, backend to frontend:

```
netlist --(ngspice, DC operating point)--> SimResult
  {node_voltages: {node: volts}, branch_currents: {refdes: amps}}
    --(rules.verify / check_rules)--> Report
  {verdict, findings, node_voltages, branch_currents,
   component_power, netlist_used, uncertain}
    --(JSON, POST /check or /run-goal or /run-image)--> overlay.js
```

Field mapping (from `Report.to_dict()` in `backend/netproof/rules.py`):

| Report field | Shown as | Where |
| --- | --- | --- |
| `node_voltages[node]` | Node voltage, volts | Label at the node |
| `branch_currents[refdes]` | Branch current, amps | Label on the component |
| `component_power[refdes]` (see note below) | Dissipated power, watts | Label on the component |
| `findings[].message` | Rule text (already states the measured number and the limit, e.g. `"R1 dissipates 0.50 W, over its 0.25 W rating"`) | Findings list / selected-component panel |
| `uncertain[]` | Checks that could not run | Shown verbatim; a PASS with `uncertain` entries is never presented as fully verified |

**Open field-name mismatch (server adapter must resolve it):** the two
backend `Report` shapes disagree on the power key. `rules.py`
`Report.to_dict()` emits `component_power`, while the pydantic `Report`
in `ir.py` — the shape `edit-panel.js` reads — defines `power`. Until the
server settles on one name, the adapter in `api.js` must map
`component_power` -> `power` (or vice versa) so the 3D layer never reads
an empty dict and shows `—` for power that was actually simulated.

Rules for the overlay:

- **Never invent a number.** If a value is absent from the `Report`
  (no sim yet, sim failed, or the check was underdetermined), the label
  shows `—`, not `0` and not a browser-side estimate.
- **No physics in the browser.** No Ohm's law, no KCL, no power math in
  any `web/3d/*.js` file. Component power comes from
  `Report.component_power` / `Report.power` (see the mismatch note above),
  which `rules.py` derives from the simulated voltages and currents
  (resistor `V^2/R` or `I^2*R`, LED `|V_anode - V_cathode| * |I_led|`,
  source `|V+ - V-| * |I|`). The one arithmetic exception is display-only:
  a component's voltage is shown as `V(n1) - V(n2)` from the Report's
  node voltages, because the Report carries node voltages, not
  per-component voltages (`edit-panel.js` does this).
- **Before the first check, the scene shows no numbers at all** — meshes
  and labels for values are empty, not zeroed.
- Node `"0"` is ground and is always `0 V` when present in the results.

## Verdict colors

The 2D verdict banner (`#verdict` in `web/index.html`) uses PASS green
`#1a7f37`, WARN amber `#d97706`, STOP red `#b00020`. The 3D meshes use
brightened variants of the same hues so emissive meshes read on the dark
scene (`VERDICT_COLORS` in `scene.js`):

| Verdict / severity | 2D banner | 3D mesh | Hex (3D) |
| --- | --- | --- | --- |
| `PASS` | `#1a7f37` | Brighter green | `#2ea043` |
| `WARN` | `#d97706` | Same amber | `#d97706` |
| `STOP` | `#b00020` | Brighter red | `#e5484d` |
| `INFO` | — | Blue | `#4da3ff` |
| `UNKNOWN` (no verdict yet) | — | Grey | `#8b949e` |

Do not invent new verdict hues: any future color change goes through
`VERDICT_COLORS` (planned: `colors.js`) so the two views stay in sync.

- The overall `Report.verdict` (any STOP finding -> `STOP`, else any WARN
  finding -> `WARN`, else `PASS`; derived in `Report`, never recomputed in
  JS) drives the scene-level verdict indication. A component with no
  findings keeps its neutral per-type body color (`COMPONENT_COLORS` in
  `scene.js`).
- Per-component highlight comes from the findings: `updateVerdictColors`
  accepts a per-component map (`{R1: 'PASS', D1: 'STOP'}`), one verdict
  plus a list of ids, or a findings array, and tints each named mesh —
  STOP red, WARN amber. The caller derives that map from
  `findings[].component_ids` (ir.py shape) / `finding.component`
  (rules.py dataclass shape). Severity order is STOP first, then WARN,
  then INFO.
- A selected component gets an outline/highlight distinct from verdict
  colors, so selection is never confused with a STOP.

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
