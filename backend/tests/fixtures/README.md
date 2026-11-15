# Test fixtures

Every JSON file in this folder is one small circuit netlist, loaded by
`backend/tests/conftest.py` (`_load_netlist`) as a `netproof.ir.Netlist`.
They are the shared inputs for the rules-engine tests: each fixture is
built to trigger (or cleanly pass) one rule, so a test can assert a
specific finding code and verdict for it.

## How to read this file

- **Circuit**: the components as written in the JSON. `value` is in SI
  units (ohms, farads, volts). For an LED, `value` is its forward
  voltage (2.0 V here); for a voltage source it is the supply voltage.
  Node `"0"` is ground. LED `nodes` are `[anode, cathode]`; source
  `nodes` are `[+, -]`.
- **Expected verdict / findings**: what `netproof.rules.verify`
  (pre-sim ERC plus post-sim checks, when a sim result is supplied)
  should report. Verdict is derived from the worst finding:
  any STOP gives STOP, else any WARN gives WARN, else PASS
  (INFO findings alone still give PASS).
- **Rule**: the finding code the fixture is named for. Some fixtures
  also trigger a second finding; those are listed, because the verdict
  follows the worst finding, not the fixture name.
- **Sim values**: hand calculations assuming the LED drops its full
  forward voltage (2.0 V), so LED current is `(9 - 2) / R` and resistor
  power is `7^2 / R`. This matches the canned sim result in
  `conftest.py` (`fake_sim_result`: 9 V, 2 V, 470 ohm gives 14.9 mA).
  Post-sim findings need node voltages / branch currents; pre-sim
  findings (ground, floating node, shorts, no source) need no sim.

## Thresholds (`netproof/rules.py`)

| Check | WARN | STOP | Other |
| --- | --- | --- | --- |
| LED current | 20-30 mA (`LED_OVERCURRENT`, WARN) | over 30 mA (`LED_OVERCURRENT`, STOP) | under 1 mA (`LED_UNDERCURRENT`, INFO) |
| Resistor power | over 70% of rating (`RESISTOR_POWER`, WARN) | over 100% of rating (`RESISTOR_POWER`, STOP) | rating defaults to 0.25 W when omitted |
| Source current | over 1 A (`SOURCE_OVERCURRENT`, WARN) | - | - |
| LED reverse bias | - | - | anode below cathode (`REVERSED_LED`, INFO) |
| No ground node `"0"` | - | `NO_GROUND`, STOP | pre-sim |
| Node with one connection | - | `FLOATING_NODE`, STOP | pre-sim |
| Source with both terminals on one node | - | `SHORTED_SOURCE`, STOP | pre-sim |
| Non-source component with both terminals on one node | `SHORTED_COMPONENT`, WARN | - | pre-sim |
| No voltage/current source | - | - | `NO_SOURCE`, INFO, pre-sim |

## Summary

| Fixture | Circuit | Expected verdict | Findings (worst first) | Rule it triggers |
| --- | --- | --- | --- | --- |
| `good_led.json` | 9 V, 470 ohm, red LED | PASS | none | (control: no rule) |
| `led_overcurrent.json` | 9 V, 100 ohm, red LED | STOP | `LED_OVERCURRENT` STOP; `RESISTOR_POWER` STOP | `LED_OVERCURRENT` |
| `no_ground.json` | 9 V, 470 ohm, red LED, no node `"0"` | STOP | `NO_GROUND` STOP | `NO_GROUND` |
| `floating_node.json` | Good LED circuit plus 1 uF cap to node `float1` | STOP | `FLOATING_NODE` STOP (node `float1`) | `FLOATING_NODE` |
| `shorted_source.json` | 9 V source, both terminals on `"0"` | STOP | `SHORTED_SOURCE` STOP | `SHORTED_SOURCE` |
| `shorted_component.json` | 9 V source, 470 ohm resistor both ends on `vcc`, plus 470 ohm resistor `vcc` to `0` | WARN | `SHORTED_COMPONENT` WARN | `SHORTED_COMPONENT` |
| `no_source.json` | 470 ohm + red LED in parallel, no source | PASS | `NO_SOURCE` INFO | `NO_SOURCE` |
| `led_undercurrent.json` | 9 V, 100k ohm, red LED | PASS | `LED_UNDERCURRENT` INFO | `LED_UNDERCURRENT` |
| `resistor_warn.json` | 9 V, 270 ohm, red LED | WARN | `LED_OVERCURRENT` WARN; `RESISTOR_POWER` WARN | `RESISTOR_POWER` |
| `reversed_led.json` | 9 V, 470 ohm, LED anode/cathode swapped | PASS | `REVERSED_LED` INFO; `LED_UNDERCURRENT` INFO | `REVERSED_LED` |
| `source_overcurrent.json` | 9 V source, 1 ohm resistor straight across it | STOP | `RESISTOR_POWER` STOP; `SOURCE_OVERCURRENT` WARN | `SOURCE_OVERCURRENT` (see note) |

## Fixture by fixture

### `good_led.json` — the passing control

- Title: `good LED 9V`.
- Circuit: V1 voltage source 9.0 V, nodes `[vcc, 0]`; R1 resistor
  470.0 ohm, `rating_watts` 0.25, nodes `[vcc, led]`; D1 red LED,
  forward voltage 2.0 V, nodes `[led, 0]`.
- Sim values: LED current `(9 - 2) / 470 = 14.9 mA`; R1 power
  `7^2 / 470 = 0.104 W`, 42% of its 0.25 W rating.
- Expected verdict: **PASS**, no findings. Current is below the
  20 mA LED warn level and above the 1 mA undercurrent level, and
  resistor power is below the 70% warn level.

### `led_overcurrent.json` — LED overcurrent

- Title: `led_overcurrent`.
- Circuit: same as `good_led.json` except R1 is 100.0 ohm
  (`rating_watts` 0.25).
- Sim values: LED current `(9 - 2) / 100 = 70 mA`; R1 power
  `7^2 / 100 = 0.49 W`, 196% of its 0.25 W rating.
- Expected verdict: **STOP**.
- Findings: `LED_OVERCURRENT` STOP on D1 (70 mA over the 30 mA
  limit), and `RESISTOR_POWER` STOP on R1 (0.49 W over its 0.25 W
  rating). Both are STOPs; the fixture triggers two rules, not one.

### `no_ground.json` — no ground node

- Title: `9V LED circuit with no ground node`.
- Circuit: V1 9.0 V, nodes `[vcc, x]`; R1 470.0 ohm (no rating given,
  so 0.25 W is assumed), nodes `[vcc, led]`; D1 red LED 2.0 V,
  nodes `[led, x]`. No component touches node `"0"`.
- Expected verdict: **STOP**, pre-sim, no sim result needed.
- Findings: `NO_GROUND` STOP. Every node has two connections, so no
  floating-node finding. Without a ground reference the operating
  point is undefined, so post-sim checks do not apply.

### `floating_node.json` — floating node

- Title: `9V LED circuit with a floating node`.
- Circuit: the `good_led.json` circuit plus C1 capacitor 1e-6 F
  (1 uF), nodes `[vcc, float1]`. Node `float1` has only this one
  connection.
- Expected verdict: **STOP**, pre-sim.
- Findings: `FLOATING_NODE` STOP on node `float1`. The LED loop
  itself is the good circuit (14.9 mA, PASS on its own); the
  capacitor is DC-open, so it does not change the operating point.
  The stop comes only from the dangling node.

### `shorted_source.json` — shorted source

- Title: `Shorted voltage source`.
- Circuit: V1 voltage source 9.0 V only, nodes `[0, 0]` (both
  terminals on ground).
- Expected verdict: **STOP**, pre-sim, no sim result needed.
- Findings: `SHORTED_SOURCE` STOP on V1. Do not simulate this: an
  ideal 9 V source shorted across itself has undefined/infinite
  current.
- Loading note: identical nodes are allowed by `ir.Component` (a
  short is an ERC finding reported by `rules.py`, not a validation
  error), so this fixture loads with plain `Netlist.model_validate`.
  `conftest.py` still lists it with the shorted fixtures and falls
  back to `model_construct` only if validation ever fails.

### `shorted_component.json` — shorted component

- Title: `Shorted resistor`.
- Circuit: V1 9.0 V, nodes `[vcc, 0]`; R1 resistor 470.0 ohm
  (no rating given), nodes `[vcc, vcc]` (both ends on `vcc`); R2
  resistor 470.0 ohm (no rating given), nodes `[vcc, 0]`.
- Expected verdict: **WARN**, pre-sim, no sim result needed.
- Findings: `SHORTED_COMPONENT` WARN on R1 (both terminals on
  `vcc`, so it is shorted out and carries no voltage, hence no
  power). No other findings: R2 gives node `"0"` a second
  connection (V1 plus R2), and `vcc` has four connections, so no
  node is floating. Even with a sim result the verdict stays WARN:
  R2 would dissipate `9^2 / 470 = 0.172 W`, 69% of its 0.25 W
  rating, just under the 70% warn level.
- Loading note: identical nodes are allowed by `ir.Component` (a
  short is an ERC finding reported by `rules.py`, not a validation
  error), so this fixture loads with plain `Netlist.model_validate`.
  `conftest.py` still lists it with the shorted fixtures and falls
  back to `model_construct` only if validation ever fails.

### `no_source.json` — no power source

- Title: `No power source`.
- Circuit: R1 resistor 470.0 ohm, nodes `[a, 0]`; D1 red LED 2.0 V,
  nodes `[a, 0]`, in parallel. No voltage or current source.
- Expected verdict: **PASS**, pre-sim.
- Findings: `NO_SOURCE` INFO only. INFO does not change the verdict.
  With no source every node voltage and branch current is 0, so if
  a sim result is supplied, `LED_UNDERCURRENT` INFO (0 mA, below
  1 mA) is also expected; the verdict stays PASS.

### `led_undercurrent.json` — LED undercurrent

- Title: `led_undercurrent`.
- Circuit: same as `good_led.json` except R1 is 100000.0 ohm
  (100k, `rating_watts` 0.25).
- Sim values: LED current `(9 - 2) / 100000 = 0.07 mA`; R1 power
  `7^2 / 100000 = 0.00049 W`, negligible.
- Expected verdict: **PASS**.
- Findings: `LED_UNDERCURRENT` INFO on D1 (0.07 mA, below the 1 mA
  minimum; the LED is likely off or too dim to see). INFO only, so
  the verdict is PASS, not WARN.

### `resistor_warn.json` — resistor power warning

- Title: `resistor_warn`.
- Circuit: same as `good_led.json` except R1 is 270.0 ohm
  (`rating_watts` 0.25).
- Sim values: LED current `(9 - 2) / 270 = 25.9 mA`; R1 power
  `7^2 / 270 = 0.181 W`, 73% of its 0.25 W rating.
- Expected verdict: **WARN**.
- Findings: `RESISTOR_POWER` WARN on R1 (0.181 W is over 70% of
  0.25 W but under 100%), **plus** `LED_OVERCURRENT` WARN on D1
  (25.9 mA is in the 20-30 mA warning range, under the 30 mA STOP
  limit). Both findings are WARNs and there is no STOP, so the
  verdict is WARN. (The old 220 ohm value gave 31.8 mA, over the
  30 mA limit, so the verdict was STOP; 270 ohm keeps the resistor
  over 70% of its rating while bringing the LED current into the
  warn band.)

### `reversed_led.json` — reversed LED

- Title: `reversed_led`.
- Circuit: same as `good_led.json` except D1 nodes are `[0, led]`
  (anode on ground, cathode on `led`, swapped from `[led, 0]`).
- Sim values: the LED is reverse-biased and does not conduct, so
  LED current is about 0 mA and, with no drop across R1, the `led`
  node sits near 9 V: anode 0 V, cathode about 9 V.
- Expected verdict: **PASS**.
- Findings, both post-sim INFO: `REVERSED_LED` INFO on D1 (anode
  0 V below cathode about 9 V), and `LED_UNDERCURRENT` INFO on D1
  (about 0 mA, below 1 mA). No pre-sim findings: ground is present
  and every node has two connections.

### `source_overcurrent.json` — source overcurrent

- Title: `source_overcurrent`.
- Circuit: V1 9.0 V, nodes `[vcc, 0]`; R1 resistor 1.0 ohm
  (`rating_watts` 0.25), nodes `[vcc, 0]`, straight across the source.
- Sim values: current `9 / 1 = 9 A` through R1 and out of V1;
  R1 power `9^2 / 1 = 81 W`.
- Expected verdict: **STOP**.
- Findings: `SOURCE_OVERCURRENT` WARN on V1 (9 A over the 1 A
  limit), **plus** `RESISTOR_POWER` STOP on R1 (81 W, far over its
  0.25 W rating). The resistor STOP sets the verdict; the fixture
  is named for the source warning.

## Notes and gotchas

- **Fixtures that trigger two rules**: `led_overcurrent.json`
  (LED stop plus resistor stop),
  `resistor_warn.json` (resistor warn plus LED warn),
  `source_overcurrent.json` (source warn plus resistor stop), and
  `reversed_led.json` (reversed-LED info plus LED undercurrent info).
  Tests should assert the full finding set, or at least the worst
  finding, not only the finding the filename suggests.
- **Shorted fixtures do not bypass IR validation**: `shorted_source.json`
  and `shorted_component.json` have a component with identical
  nodes, which `ir.Component` allows (a short is an ERC finding
  reported by `rules.py`, not a validation error), so both load with
  plain `Netlist.model_validate`. `conftest.py` still lists them as
  shorted fixtures and falls back to `model_construct` only if
  validation ever fails.
- **`rating_watts` naming**: fixtures write `rating_watts`, which
  is the field name in `ir.Component` (resistors default to 0.25 W
  when it is omitted, as in `no_ground.json`,
  `shorted_component.json`, and the resistor in `no_source.json`).
  `parts.resistor_rating_w` (used by `rules.py`) reads `rating_watts`
  from both `Component` objects and raw dicts, falling back to the
  same 0.25 W default only when it is omitted; the numbers in this
  file are unaffected either way, because every fixture that states
  a rating states 0.25 W.
- **WARN-verdict fixtures**: `resistor_warn.json` and
  `shorted_component.json` are the two WARN cases (each has only
  WARN findings, no STOP). The set covers PASS (`good_led`,
  `no_source`, `led_undercurrent`, `reversed_led`) and STOP (the
  other five).
- **Adding a fixture**: add the JSON here, add a loader fixture in
  `backend/tests/conftest.py`, document it in the summary table and
  in its own section above (filename, circuit, hand-calculated sim
  values, expected verdict and full finding list, rule triggered),
  and check for second findings the way the notes above do before
  writing the expected verdict.
