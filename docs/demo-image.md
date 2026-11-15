# Demo image: how to make it

File owner: `docs/demo-image.md` ONLY. This file is instructions only.
It does not contain an image, and no image was generated for this task.
Do NOT create `backend/tests/fixtures/demo_led_undersized.png` from
anything fake: no AI-generated image, no stock photo, no screenshot of
a different circuit. The PNG only exists once Nayan makes a real
schematic of the circuit below and saves it under that name.

## The circuit (must match this exactly)

One series loop, same as `backend/tests/fixtures/led_overcurrent.json`
and `backend/tests/fixtures/good_led.json`:

| Ref | Part | Value | Nodes |
| --- | --- | --- | --- |
| V1 | Voltage source | 9 V | `vcc` to `0` (ground) |
| R1 | Resistor, 1/4 W rating | 100 ohm | `vcc` to `led` |
| D1 | Red LED | Vf 2.0 V | `led` (anode) to `0` (cathode, ground) |

Topology: 9 V source -> 100 ohm resistor -> red LED -> ground, back to
the source. LED anode faces the resistor, cathode faces ground. Do not
reverse the LED, do not add parts, do not change a value.

## How to make the image (pick one)

1. **Hand drawing (fastest):** draw the loop on paper with the
   standard symbols (battery / 9 V source, resistor zigzag or box,
   LED triangle + bar with light arrows), label every part `V1 9V`,
   `R1 100 ohm`, `D1 red LED`, mark `+` / ground, and take a straight,
   well-lit photo. Crop to the schematic.
2. **Drawn in an editor:** draw the same loop in any schematic / drawing
   tool and export PNG.
3. **Datasheet / textbook screenshot:** only if it shows exactly this
   circuit with these values. A screenshot of a different circuit, a
   different resistor, or a different LED does not count, even if it
   looks similar. If the source circuit differs, annotate / redraw it
   so the values above are what is shown; do not leave wrong values
   visible.

## Legibility checklist (check before you save)

- [ ] All three labels readable: `9V`, `100 ohm`, `red LED`.
- [ ] Refdes readable: `V1`, `R1`, `D1`.
- [ ] LED direction readable: anode toward R1, cathode toward ground.
- [ ] One closed loop, no extra components, no cut-off edges.
- [ ] PNG, not a blurry photo of a screen. If text is unreadable at
  normal zoom, retake / redraw it.

## Save it

Save the real image as:

```
backend/tests/fixtures/demo_led_undersized.png
```

That is the demo fixture name. Keep the original drawing / photo as
well, so the PNG can be remade if it is ever questioned.

## Upload and run it

1. Start the app (`uv run uvicorn netproof.server:app --reload`,
   then open the web UI).
2. Drag and drop `demo_led_undersized.png` onto the dropzone
   (or click it to choose the file). This POSTs to `/run-image`.
3. Run it. The netlist the app extracts must be the circuit above:
   V1 9 V, R1 100 ohm, D1 red LED. If the extracted netlist differs,
   fix the image (usually a misread label), not the numbers below.

## Expected result, step 1: STOP

Verdict **STOP**. These are the rules that must fire
(`backend/netproof/rules.py`):

- `LED_OVERCURRENT` (STOP): D1 current over the 30 mA limit
  (warn starts at 20 mA).
- `RESISTOR_POWER` (STOP): R1 dissipates over its 0.25 W rating.

### Expected numbers (ngspice-derived, not hardcoded app results)

These numbers come from solving the circuit with the simulator's own
models, not from anything the app prints. Two consistent ways to get
them, both agree on the verdict:

**A. Full SPICE diode model** (`backend/netproof/parts.py`:
`.model DRED D(IS=1e-18 RS=5 N=2.0 BV=5 IBV=1e-3)`, solved as
`9 = I x (R1 + 5) + 2.0 x 0.02585 x ln(I / 1e-18)` at 27 C):

| | Bad circuit, R1 = 100 ohm |
| --- | --- |
| D1 current | ~66.6 mA |
| D1 voltage | ~2.34 V |
| Voltage across R1 | ~6.66 V |
| R1 power `I^2 x R` | ~0.44 W |

**B. Fixed-Vf hand check** (same tuned model: Vf is ~2.34 V at
~67 mA, so use that Vf, not the 2.0 V nominal from `led_vf("red")`,
which only matches at the 15 mA reference current):

| | Bad circuit, R1 = 100 ohm |
| --- | --- |
| D1 current `(9 - 2.34) / 100` | ~66.6 mA |
| Voltage across R1 | ~6.66 V |
| R1 power `0.0666^2 x 100` | ~0.44 W |

Either way D1 is far over 30 mA and R1 is far over 0.25 W, so STOP is
overdetermined. Judge the demo on the verdict and the rule codes, not
on hitting one exact milliamp number: accept roughly 65-70 mA for D1
in the bad circuit. The exact value depends on which diode model
ngspice uses; the verdict does not.

### Correction: do NOT use ~48 mA / ~0.31 W

An earlier brief quoted D1 ~48 mA and R1 ~0.31 W for the bad circuit.
Those two numbers do not go together and do not match this circuit:

- 48 mA through 100 ohm gives R1 power `0.048^2 x 100 = 0.23 W`,
  not 0.31 W. And 48 mA with a ~2 V LED implies a supply of
  `2 + 0.048 x 100 = 6.8 V`, about 7 V, not the 9 V in this circuit.
- 0.31 W through 100 ohm implies `sqrt(0.31 / 100) = 55.7 mA`,
  not 48 mA.

Use ~67 mA / ~0.44 W above instead. If a run ever reports
~48 mA for this circuit, something is wrong with the extracted netlist
or the diode model, not with the expectation.

### How to confirm on a Mac (ngspice was not installed in this VM)

```bash
brew install ngspice
```

Then run the repo's own simulator on the matching fixture
(`backend/netproof/spice.py`: `netlist_to_spice` + `run_ngspice`)
against `backend/tests/fixtures/led_overcurrent.json`. The SPICE deck
it builds is:

```
* Netproof
.model DRED D(IS=1e-18 RS=5 N=2.0 BV=5 IBV=1e-3)
V1 vcc 0 DC 9
R1 vcc led 100
D1 led 0 DRED
.op
.control
op
print all
.endc
.end
```

## Expected result, step 2: Repair to 470 ohm, then PASS

Click **Repair** (POST `/repair`), or edit R1 in the netlist table
from 100 to 470 ohm and click **Re-check** (POST `/check`, no LLM).
The repaired circuit is `backend/tests/fixtures/good_led.json`:
V1 9 V, R1 470 ohm, D1 red LED.

Expected numbers for the repaired circuit:

| | Repaired, R1 = 470 ohm |
| --- | --- |
| D1 current, SPICE diode model | ~14.9 mA |
| D1 current, fixed Vf 2.0 V: `(9 - 2) / 470` | 14.9 mA |
| R1 power, SPICE model | ~0.10 W |
| R1 power, fixed Vf: `0.0149^2 x 470` | ~0.10 W |
| D1 voltage | ~2.00 V (SPICE model) / 2.0 V (fixed-Vf check) |

Verdict **PASS**: no findings. ~15 mA is under the 20 mA warn
and 30 mA stop limits and over the 1 mA undercurrent floor, and
~0.10 W is under R1's 0.25 W rating (and under the 70% warn
level of 0.175 W).

## If the demo does not match

- **STOP does not fire on the bad circuit:** check the extracted
  netlist first. Wrong R1 (e.g. read as 1k), a reversed LED, or a
  missing ground changes the current completely. Fix the image.
- **Repaired circuit still STOPs:** check R1 really became 470 ohm
  in the table before re-checking. 100 ohm left in place still STOPs.
- **Numbers differ slightly from the table above:** that is fine if
  the verdict and rule codes match. The two derivations above now
  agree within ~1 mA on the bad circuit; ngspice's exact diode solve
  sits in that range. What is not fine is a number that implies
  impossible physics (see the 48 mA / 0.31 W note above).
- **Image upload fails:** the file must be a real PNG/JPG/SVG of the
  schematic above. A renamed non-image file, or a photo with no
  schematic in it, is not a demo failure of Netproof, it is a bad
  input file.
