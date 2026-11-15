# Netproof

Netproof is a circuit-checking tool for hobby electronics. You describe a
circuit (or paste a netlist), and Netproof tells you whether it is safe to
build — before you wire anything up.

The core principle: **the LLM proposes, the simulator decides.** A language
model may suggest a circuit, explain a problem, or propose a repair, but
every verdict comes from an actual SPICE simulation (ngspice), checked
against a fixed set of rules. The model never gets the final say on whether
a circuit passes.

## How it works

1. You submit a circuit (netlist / component list) through the API or web UI.
2. Netproof builds a SPICE netlist and runs a DC operating-point simulation
   with ngspice.
3. The simulation results are checked against the rule list below.
4. You get a verdict — **PASS**, **WARN**, or **STOP** — plus the specific
   rule(s) that fired and, on a STOP, a proposed repair. A repair is only
   accepted if the repaired circuit re-simulates clean. The verdict is
   derived from the findings: any STOP finding means STOP, otherwise any
   WARN finding means WARN, otherwise PASS.

## Rules

Every verdict is produced by one or more of these rules, evaluated on
the netlist and, where noted below, on simulation output:

| Rule | Severity | Trigger |
| --- | --- | --- |
| `NO_GROUND` | STOP | No ground (reference node) in the circuit |
| `FLOATING_NODE` | STOP | A node is not connected to anything that defines its voltage |
| `SHORTED_SOURCE` | STOP | A voltage source is shorted out |
| `SHORTED_COMPONENT` | WARN | A component is bypassed by a short |
| `NO_SOURCE` | INFO | No power source in the circuit |
| `LED_OVERCURRENT` | STOP over 30 mA, WARN at 20-30 mA | LED current exceeds its safe limit |
| `LED_UNDERCURRENT` | INFO | LED current below 1 mA, too low for the LED to light usefully |
| `REVERSED_LED` | INFO | An LED is wired backwards (reverse-biased) |
| `RESISTOR_POWER` | STOP over 100% of rating, WARN over 70% | A resistor dissipates more power than its rating (default rating 0.25 W) |
| `SOURCE_OVERCURRENT` | WARN | A source is asked to supply more than 1 A |

Severities and thresholds are the ones in `backend/netproof/rules.py`.
`LED_OVERCURRENT`, `LED_UNDERCURRENT`, `REVERSED_LED`,
`RESISTOR_POWER`, and `SOURCE_OVERCURRENT` need a simulation result;
the rest are static checks that run even without one.

## Run it

Prerequisites: [Homebrew](https://brew.sh) (macOS) or your distro's package
manager (Debian/Ubuntu: `apt install ngspice`), [uv](https://docs.astral.sh/uv/),
and an Anthropic API key. Run from the repo root, where `pyproject.toml` lives.

```bash
# 1. Install the simulator
brew install ngspice

# 2. Install dependencies
uv sync

# 3. Check the install
which ngspice
uv run pytest

# 4. Set your API key
export ANTHROPIC_API_KEY=sk-ant-...

# 5. Start the server (backend/netproof/server.py exists; it serves
#    the API and the web UI on the same origin)
uv run uvicorn netproof.server:app --reload
```

The server refuses to start without `ngspice` on PATH (its startup
check raises `RuntimeError` with install instructions), unless a fake
simulator has been injected via `server.set_simulator` (tests only).
`uv run pytest` runs the full suite: the server tests use that fake
simulator, so they run without ngspice; the one real-ngspice test
(`test_pipeline.py::test_real_ngspice_good_netlist_passes`) skips when
the binary is absent.

Environment variables:

| Variable | Required | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Yes for LLM features | Anthropic API key, used for circuit explanations and repair proposals. The simulator, not the model, decides the verdict. The `/check` verdict path makes no LLM call, so simulation-only checks do not need the key. |

## Demo script

The canonical demo is a 9 V LED circuit with an undersized series resistor
(see the test fixtures `backend/tests/fixtures/led_overcurrent.json` and
`backend/tests/fixtures/good_led.json`):

1. **Submit the bad circuit.** A 9 V source, a red LED, and a 100 Ω
   series resistor. The tuned diode model (Vf ≈ 2.34 V at this
   current) lets about (9 − 2.34) V / 100 Ω ≈ 67 mA flow, over the
   30 mA STOP line, with R1 dissipating about 0.44 W, over its
   0.25 W rating.
2. **STOP.** Netproof simulates it and reports `LED_OVERCURRENT` — the LED
   would burn out. The verdict comes from the simulation, not from the
   model's opinion.
3. **Repair.** Netproof proposes a correctly sized series resistor: 470 Ω,
   which lets about (9 − 2) V / 470 Ω ≈ 15 mA flow (14.9 mA simulated,
   Vf ≈ 2.00 V at that current).
4. **Re-simulate.** The repaired circuit is simulated again.
5. **PASS.** No rules fire. The circuit is safe to build.

## Project layout

```
netproof/
├── README.md            # This file
├── pyproject.toml       # uv project root: dependencies, pytest config
├── backend/
│   ├── netproof/        # Python package: ir, spice (ngspice), rules,
│   │                    # parts, bom, llm, pipeline, server
│   └── tests/           # conftest.py, test_server.py, test_pipeline.py,
│                        # test_spice.py, test_rules.py, test_llm.py,
│                        # test_consensus.py, test_bom.py, test_3d.py,
│                        # fixtures/
└── web/
    ├── index.html       # Web UI: input, verdict banner, netlist table,
    │                    # 3D view container + guarded dynamic import
    │                    # of ./3d/main3d.js
    └── 3d/
        ├── main3d.js    # 3D entry point (init3D)
        ├── scene.js     # Three.js scene, camera, picking
        ├── components.js # Component meshes + layout
        ├── interaction.js # Orbit / pick / drag / double-click
        ├── overlay.js   # Live-number overlays on meshes
        ├── edit-panel.js # Edit panel for the picked component
        ├── history.js   # Repair-history timeline (UMD)
        └── README-3D.md # 3D layer spec
```

## 3D view

`web/3d/` is a frontend-only 3D view of the circuit. It runs entirely in
the browser and never talks to the backend, the simulator, or the LLM
directly.

- **Library:** Three.js 0.160.0 via CDN
  (`https://unpkg.com/three@0.160.0/build/three.module.js`, plus
  `OrbitControls` from the same version). No build step.
- **Navigate:** left-drag orbits, right-drag pans, scroll wheel zooms
  (`OrbitControls`, damping on). Touch: one finger rotates, two fingers
  zoom/pan.
- **Click:** clicking a component selects it (raycast pick; a pointer
  release only counts as a click if it moved 5 px or less, so an orbit
  drag never selects by accident). Clicking empty space clears the
  selection. The host page gets the pick through an `onPick` callback.
- **Drag:** dragging orbits the camera; it does not move components.
- **Edit:** double-click is the edit gesture: it opens the edit panel
  (`web/3d/edit-panel.js`), where its value, power rating, and (for LEDs)
  colour can be changed. Saving posts the edited netlist to `/check`
  (or `/repair`) and re-simulates; the panel itself never computes a
  verdict. The wiring exists end to end: `interaction.js` fires `onEdit`
  on double-click, `main3d.js` opens the edit panel, and the host page's
  `onEdit` callback applies the patch and re-checks.
- **Numbers come from ngspice only.** The 3D view holds no simulation
  numbers of its own. Mesh colours come only from the verdict values
  passed in by the host page, which must come from a real `/check` run
  (PASS green, WARN amber, STOP red). Voltages, currents, and power shown
  in the edit panel come only from that `/check` report; a value missing
  from the report is shown as "—", never guessed or filled in.

Status: wired into `web/index.html` — the page carries an import map
mapping `three` to the same 0.160.0 URL, a `#scene3d` container, and a
guarded dynamic import of `./3d/main3d.js` (in `try/catch`: if the import
or init fails, the flat page keeps working and the 3D container shows
the failure instead of an error). All seven `web/3d/*.js` modules exist
(`main3d.js`, `scene.js`, `components.js`, `interaction.js`,
`overlay.js`, `edit-panel.js`, `history.js`). The server serves the
whole `web/` directory same-origin (static mount after the API routes),
so the `./3d/main3d.js` import resolves instead of 404ing. Live
in-browser rendering is not covered by the backend test suite; the
tests cover serving the files, not running them.

## Known limits

- **Server needs ngspice to start.** `backend/netproof/server.py` exists
  and serves the API plus the web UI, but its startup check refuses to
  start without the `ngspice` binary on PATH (tests inject a fake
  simulator instead, so they run without it).
- **3D view is wired in but not browser-verified here.** `web/index.html`
  loads `./3d/main3d.js` and the server serves it, but no backend test
  runs the 3D code in a browser, so a rendering failure would surface
  only in the `#scene3d` fallback text, not in the test suite.
- **DC operating point only.** Netproof runs a DC operating-point
  simulation. It does not do transient analysis or AC analysis, so it
  cannot check anything that depends on time, switching, or frequency
  (capacitor charging, oscillation, filters, PWM, and similar).
- **Simulation is only as good as the model.** Results depend on the
  component values and models supplied. Wrong values in, wrong verdict out.
- **No accuracy claims.** Netproof makes no claims about how often its
  verdicts match a bench measurement, and no benchmark numbers are stated
  anywhere in this project. The guarantee is narrower and checkable: every
  verdict is derived from a real simulation run against the rule list
  above, never from the language model alone.
