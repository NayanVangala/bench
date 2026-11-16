# bench. — handoff for the next agent

Read ALL of this before touching anything. Every rule here exists because something broke.

---

## 1. What this is

**bench.** is a hackathon web app (Bay Valley Hacks). A user designs one of three things, checks it with real physics, and downloads a build pack PDF (parts, prices, where to buy, how to assemble):

| Mode | What it is | Physics engine (deterministic) |
|---|---|---|
| Circuit | breadboard / PCB circuits | ngspice DC operating point + rules in `rules.py` |
| Fiber link | laser → fiber/connectors/splitters → detector | optical power budget in `fiber.py` |
| Optics bench | free-space laser, mirrors, beamsplitters, lenses, detectors on a 25 mm breadboard | 2D ray trace in `optics.py` |

**Core principle: "the AI proposes, the simulator decides."** The AI (Claude Haiku 5.5) only proposes designs or reads images. Verdicts (PASS / WARN / STOP), scores and tuner decisions are ALWAYS computed by deterministic code. Never let the model decide a verdict, never fake a simulation result, never show a number in the UI that the server didn't compute.

- Live: https://netproof-quyv.onrender.com (Render free plan, Docker, auto-deploys on push to `main`; first load after idle takes ~50 s because free instances sleep)
- Repo: https://github.com/NayanVangala/bench (private). Local path: `~/netproof` (the folder name is old; the product is "bench.").
- The owner works on **desktop only**. Do NOT do mobile/phone layout work. Do NOT delete the existing responsive CSS either. Just leave it alone.

---

## 2. Hard rules (break these and you break the project)

1. **Never `git push --force`. Never re-clone into a subfolder.** A previous agent force-pushed and nested the repo inside itself (`~/netproof/netproof`). That cost hours. Work in `~/netproof` directly, commit normally, `git pull --rebase` if push is rejected.
2. **All tests must pass before every commit**: `UV_CACHE_DIR=$TMPDIR/uvcache uv run pytest -q` → currently `268 passed, 1 skipped`. If you change behavior, update/add tests in `backend/tests/`.
3. **Keep `uv.lock` committed.** The Render Docker build fails without it.
4. **Don't change `pyproject.toml` packaging** (`[tool.hatch.build.targets.wheel] packages = ["backend/netproof"]`). The old `force-include` installed a stale copy of the package and caused fake 405 errors.
5. **No frontend build step.** Plain HTML/CSS/JS in `web/`. Three.js 0.160.0 comes from an import map in `web/index.html`; modules import bare `'three'`. Don't add npm, bundlers, React, Tailwind, etc.
6. **Never use `window.alert/confirm/prompt`.** They block browser automation (Playwright). Use inline UI (there is already an inline title input and a two-step delete).
7. **Model IDs**: Anthropic model is `claude-haiku-5-5` in `backend/netproof/llm.py` (`MODEL`). Use the exact string, with no date suffix. `MAX_TOKENS = 16000`, because Haiku 5.5 thinks by default and thinking counts toward max_tokens. Don't lower it.
8. **Secrets**: `.env` is local only (gitignored). Never commit keys. Never print them in logs.
9. **Don't touch the KiCad VRML loader hack** (`web/3d/models.js` renames `DEF`/`USE` names containing `-`, because VRMLLoader rejects hyphens).

---

## 3. Run it locally

```bash
cd ~/netproof
uv sync                                   # first time
set -a && . ./.env && set +a              # loads ANTHROPIC_API_KEY, LLM_PROVIDER, etc.
uv run uvicorn netproof.server:app --port 8765
# open http://127.0.0.1:8765
```
- `uvicorn --env-file` fails in some sandboxes (EPERM), so source `.env` as shown above.
- ngspice must be installed (`brew install ngspice`) for circuit checks. The Docker image installs it.
- Local auth: if `AUTH_REQUIRED=1` you must sign up (email + password works without OAuth). Local DB = SQLite; Render = Postgres via `DATABASE_URL`.
- Tests: `UV_CACHE_DIR=$TMPDIR/uvcache uv run pytest -q`. Tests use fake LLM clients and need no API key or network.
- The server sends `Cache-Control: no-cache` for js/css/html, so a normal reload picks up frontend edits. Python edits need a server restart (no `--reload` by default).

### Env vars (Render → service "netproof" → Environment)
| Var | Value | Notes |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | If it's `gemini`, the app uses Gemini and Haiku isn't used at all. **Verify this on Render.** |
| `ANTHROPIC_API_KEY` | key | `sync:false` in render.yaml, so it must be set by hand in the dashboard |
| `GEMINI_API_KEY` | optional | only if provider is gemini |
| `NETPROOF_AGENTS` | `5` | AI team size (circuit readers; fiber/optics designers capped at 4) |
| `AUTH_REQUIRED` | `1` | every page needs login |
| `DATABASE_URL` | from `bench-db` | auto from Blueprint |
| `SECRET_KEY` | generated | signs session cookies |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | **NOT SET YET** | GitHub login stays hidden until set. OAuth app callback URL: `https://netproof-quyv.onrender.com/auth/github/callback` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | not set (owner doesn't want Google right now) | |

`sync:false` vars in `render.yaml` are NOT filled in by the Blueprint. Add them manually in the Render dashboard.

---

## 4. Code map

### Backend (`backend/netproof/`)
| File | What it does |
|---|---|
| `server.py` | FastAPI app. All routes, SSE streaming (`_run_streamed` runs work on a thread, streams events, sends a heartbeat every 2 s), AI rate-limit middleware (`_AI_PATHS`), static mount of `web/`, no-cache middleware. |
| `llm.py` | Anthropic/Gemini adapter. `MODEL`, `MAX_TOKENS`, `_call_model`, `_response_text` (reads blocks by type, raises a clear error on `stop_reason == "refusal"`), `read_schematic`, `design`, `repair`. |
| `pipeline.py` | Circuit pipeline: `run_image` (N parallel readers, majority consensus via `consensus_netlists`, simulate, repair loop), goal mode = best-of-N designs. `N_CANDIDATES` comes from `NETPROOF_AGENTS` (default 3). |
| `design_ai.py` | Fiber/optics AI. `propose()` = one designer (validates against the schema, one retry). `propose_team()` = **multi-agent**: 3 analysts (Parts / Path / Values) read the image in parallel, then up to 4 designers (Faithful / Simple / Safe / Performance) propose in parallel. Each candidate is ray-traced or budgeted and scored, and the best score wins. Emits per-agent events. |
| `tuner.py` | Deterministic optimizer (BEAMTEAM-style coordinate search; a step is kept only if the score strictly improves). `tune_circuit` (E12 resistor values on LED/diode nodes plus power ratings), `tune_fiber` (polish fix, attenuator sizing), `autofix_optics` (dumps for stray beams, ND filters for overload), `tune_optics` (mirror/BS angles, splits). Scores are 0–100. Every try emits `tune: "kept"|"rejected"`. |
| `fiber.py`, `optics.py` | Physics engines plus rule findings. |
| `rules.py`, `spice.py`, `ir.py`, `parts.py`, `bom.py` | Circuit rules, ngspice runner, netlist schema, parts DB, BOM. |
| `buildpack.py` + `fonts/` | Build pack PDFs (fpdf2, DejaVu fonts): `circuit_pack` / `fiber_pack` / `optics_pack` → `render_pdf`. |
| `accounts.py` | Users and saved builds (SQLAlchemy Core), scrypt passwords, HMAC cookie `bench_session`, GitHub/Google OAuth, `auth_gate` middleware, `/api/builds` CRUD. |

### SSE event shape (what the stats dock reads)
```json
{"stage": "model|wait|retry|parse|verify|agent|tune|autofix|done|error",
 "agent": "Analyst Parts|Designer Safe|Judge|Physics|Tuner|Reader 3|reader|verifier|...",
 "state": "working|done|failed",
 "message": "human text",
 "score": 99.5, "calls": 1, "physics": 1,
 "tune": "kept|rejected", "tick": true}
```
`tick: true` means "replace the previous tick line" (used for rejected tuner tries so they don't flood the log).

### Frontend (`web/`)
| File | What it does |
|---|---|
| `index.html` | Whole app page plus circuit-mode JS inline. Start screen with 3 bubbles (circuit / fiber / optics), mode bar, panels `#panel-check` / `#panel-fiber` / `#panel-optics`, tuner cards `[data-tuner]`, stats dock `#synth`, live knob `#knob`, builds dialog. |
| `styles.css` | Everything. Tokens on `:root`. Dock styles under "Synthesis log". |
| `synth.js` | `BenchSynth` (stats dock: agent lanes, counters, sparkline, full log, image overlay, fly-in) and `BenchTuner` (POST `/tune/<mode>` streamed). |
| `fiber.js`, `optics.js` | Mode logic. `NetproofDesign` helper streams `/{domain}/design/stream`. |
| `start.js`, `account.js`, `live.js` | Bubbles start screen, login/user menu/saved builds, live value knob (ngspice re-sim on drag). |
| `login.html` | Login/signup page. |
| `3d/main3d.js` | Scene handle: `setNetlist`, `setFiberLink`, `setOpticsBench`, `showReference`, `fadeReference`, `flyIn`, `resetView`, `select`. |
| `3d/components.js`, `models.js`, `fiber3d.js`, `optics3d.js`, `livefx.js`, `interaction.js`, `overlay.js`, `callouts.js`, `scene.js` | Part meshes, KiCad models, fiber pulses, optics photons, LED glow / resistor heat, drag/select, current-flow dots, callouts, camera frustum inset. |

---

## 5. Full test checklist (do it in this order, on desktop Chrome, 1440×900)

Test locally first, then on the live URL after deploy. For each step, write down PASS/FAIL plus a screenshot if it fails.

### A. Auth and builds
1. Open the site logged out → redirected to `/login`.
2. Sign up with email + password → land on the start screen.
3. Log out, log back in → works. Wrong password → clear error, no crash.
4. GitHub button is hidden until the env vars are set; after they're set, the full GitHub login round trip works.
5. Make any design → Save (inline title box, no browser popup) → it appears in My builds → reopen it → same design. Delete uses the two-step confirm.

### B. Circuit mode
6. Start screen → click the Circuit bubble → describe "blink-free LED on 9V with a resistor" → Enter → the dock opens at the bottom, lanes show agents working, verdict appears, parts fly in.
7. Upload a schematic image → image floats over the 3D grid → readers appear as lanes (`Reader 1..N` on Render = 5) → consensus → simulation → verdict.
8. Upload `pcbwaygay.png` (an nRF board): you'll only get passives because ICs/transistors aren't modeled. This is the known gap (see backlog #2).
9. Click a resistor → live knob → drag → current/power update, LED glow brightens/dims, verdict updates.
10. Run the tuner → dock shows every try (rejected = one updating grey line, kept = green line), counters go up (Tuner tries, Kept / rejected, Physics runs), score start → best.
11. Download the build pack PDF → opens, has prices, buy links, assembly steps.

### C. Fiber mode
12. Fiber bubble → describe "1550 nm 1 mW laser over 2 km SMF with two APC connectors into a photodiode" → 4 designer lanes run in parallel → Judge picks one (green outline) → Physics verdict → pulses animate along the fiber, dimming with loss.
13. Upload a fiber diagram image → 3 analyst lanes appear first (Parts/Path/Values), then the designers.
14. Tuner with a target dBm → attenuation tries stream, kept/rejected counts.
15. Build pack PDF.

### D. Optics mode
16. Optics bench → describe "532 nm 4 mW laser, two steering mirrors, 50:50 beamsplitter, two detectors" → team runs (~10 s locally) → PASS → photons stream along the beams.
17. Examples: "Mirror mis-angled", "Unblocked split arm", "20 mW into a detector" → each should give STOP/WARN with the right rule (`DETECTOR_NO_LIGHT`, `STRAY_BEAM`, `DETECTOR_OVERLOAD`) → tuner autofix adds a dump / ND filter → verdict improves.
18. Drag parts on the breadboard → they snap to 25 mm holes → beams re-trace. Rotate buttons work.
19. Build pack PDF (Thorlabs parts, hole positions, alignment steps).

### E. Stats dock
20. Lanes: dot colors (yellow pulsing = working, green = done, red = failed), message, score, seconds. Winner outline on the chosen designer.
21. Stats: Elapsed ticks every 0.25 s; AI calls, Candidates checked, Physics runs, Tuner tries, Kept / rejected, Score, Events all go up.
22. "–" collapses the dock to one line; "×" closes it and removes the reference image.
23. Kill the API key (or use a bad one) → run a design → lanes go red with the error text, the dock title says "Stopped", the page doesn't hang.

### F. Robustness
24. Hit the AI routes fast (5+ runs in a minute) → rate limit message, not a 500.
25. Huge/garbage image → clean error message.
26. Refresh mid-run → no stuck state on reload.
27. Render cold start → the first request is slow but succeeds.

---

## 6. Build backlog (priority order)

1. **Render config (owner does this, 5 min):** confirm `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` are set, add `NETPROOF_AGENTS=5` if it's missing, add `GITHUB_CLIENT_ID/SECRET`. Then redeploy.
2. **Unmodeled-parts warning (circuit image runs):** when the reader sees ICs, transistors, crystals or connectors that the circuit model can't simulate, list them in the log/dock and in the verdict panel ("Not simulated: U1 nRF52, Q1, Y1") instead of silently dropping them. Probably: have `read_schematic` also return an `unmodeled: [{id, type, note}]` list, pass it through `pipeline.run_image`'s result, and render it in `index.html`. Add a test with a fake client.
3. **Guest login for judges:** a "Try it as guest" button on `/login` that creates a throwaway user (random email, no password) and sets the session. Judges won't want to sign up. Rate-limit it. Test in `test_accounts.py`.
4. **Keep-warm pinger:** the Render free plan sleeps after 15 min. Add `.github/workflows/keepwarm.yml` with cron `*/10 * * * *` that runs `curl -fsS https://netproof-quyv.onrender.com/health`. (GitHub cron can be delayed; UptimeRobot is the alternative.) Only during judging week.
5. **Judge README:** top of `README.md`: one-line pitch, live link, guest login, a 60-second "try this" script (the optics prompt from D16 plus one example), a screenshot/GIF of the stats dock, and the architecture in 5 bullets (AI team → physics verdict → deterministic tuner → build pack). Link a demo video.
6. **Demo video (owner records):** optics prompt → team lanes → Judge → photons → tuner → PDF. ~60 s.
7. **Vision quality option:** Haiku may misread dense images. Add an optional `VISION_MODEL` env (e.g. `claude-sonnet-5-5`) used only for image reads (`read_schematic` and analysts), with everything else on Haiku. Default to unset = Haiku.
8. **Circuit goal mode as a real team:** it's currently best-of-N with the same prompt. Give the circuit designers strategies like `design_ai.DESIGNERS` (Faithful / Simple / Safe / Low-power), emit per-designer `agent/state/score` events like `propose_team`, so the dock shows lanes for circuits too.
9. **Tests to add:** a server SSE test that `/optics/design/stream` emits Judge + Physics events; a tuner test that rejected tries carry `tune: "rejected"` and `tick: True`; consensus with 5 runs (a part seen in 2/5 must be excluded, 3/5 included).
10. **Dock polish (small):** Judge/Physics lanes show `0.0s` (they're instant; show the time since run start instead or hide it). The circuit pipeline's lowercase agent names (`reader`, `verifier`) should be capitalised for display.

---

## 7. Gotchas you WILL hit

- **"Why is my JS change not showing?"** Hard reload. If it's still stale, check that you edited `~/netproof/web/...` and not some other copy.
- **405 / old behavior after a Python change** → restart uvicorn. If it's still old, someone broke the packaging (rule 4).
- **Playwright hangs** → something called `alert/confirm/prompt`. Remove it.
- **3D parts overlap** → circuit layout uses `loopLayout` in `components.js`. Don't replace it with naive positions.
- **Drag jumps** → the grab offset must be measured on the ground plane (see `interaction.js`), not on the mesh surface.
- **Selection box stuck after switching modes** → `replaceMeshes` must reconcile the selection.
- **Anthropic responses** can start with `thinking` blocks. Always read blocks by `type`, never `content[0].text`. Haiku 5.5 rejects `temperature`, `top_p`, `top_k`, `budget_tokens` and assistant prefill with a 400. Don't add them.
- **More agents = more API calls.** One fiber/optics image run ≈ 3 analysts + 4 designers (+ retries) ≈ 7–11 Haiku calls. Cheap, but the rate limiter counts requests, not calls.
- **Commit messages:** `feat: ...`, `fix: ...`, `chore: ...`. Push to `main` (auto-deploys). Check the Render deploy log if the site breaks.

---

## 8. Recent history (newest first)

- `beba73e` feat: stats dock with live agent lanes and tuner trace
- `49c3f65` feat: multi-agent AI teams (analysts + strategy designers + judge; majority consensus for N readers)
- `d3085f3` chore(llm): use Claude Haiku 5.5 (was Sonnet 5.5), max_tokens 16000, refusal error
- `fc537b5` feat: live physics on the bench (LED glow, resistor heat, live knob, fiber pulses, optics photons)
- earlier: BEAMTEAM-style synthesis view and tuner, accounts and saved builds (Postgres), bubbles start screen, rename to bench., build-pack PDFs, optics bench, fiber checker, KiCad 3D models, Render deploy

---

## 9. Swarm rules (Muse runs 100-agent swarms; read this or the repo dies)

Parallel agents on one repo cause most of this project's breakage (force-push, nested clone, a test file written against modules that didn't exist yet). With swarms:

1. **One coordinator owns `main`.** Workers never push to `main`. Each worker gets its own branch plus a git worktree (`git worktree add ../bench-wt-<task> -b swarm/<task>`). The coordinator merges one branch at a time, runs the full test suite after each merge, and pushes. Never force-push.
2. **One task = one worker = an explicit file list.** Assign each worker the exact files it may edit. Two workers must never own the same file. Hot files that ONLY the coordinator edits (or one worker per merge round): `web/index.html`, `web/styles.css`, `backend/netproof/server.py`, `pyproject.toml`, `uv.lock`, `render.yaml`.
3. **Map backlog tasks to owners** (section 6), for example:
   - #2 unmodeled parts → `llm.py`, `pipeline.py`, `tests/test_pipeline.py` (+ coordinator does the `index.html` UI bit)
   - #3 guest login → `accounts.py`, `login.html`, `tests/test_accounts.py`
   - #4 keep-warm → `.github/workflows/keepwarm.yml` only
   - #5 README → `README.md` only
   - #7 vision model → `llm.py` (so it can't run in parallel with #2; serialize them)
   - #8 circuit team → `pipeline.py` (serialize after #2)
   - #9 tests → new test files only
   - #10 dock polish → `web/synth.js` only
4. **Don't use 100 agents for 10 tasks.** Extra agents with no file ownership just create merge conflicts. Use 1 worker per backlog item, plus test/QA agents that are **read-only** (they run the checklist in section 5 and report; they don't edit).
5. **Every worker before handing back:** rebase on the latest `main`, run `UV_CACHE_DIR=$TMPDIR/uvcache uv run pytest -q` (must be all green), and report the changed files plus the test count. A failing branch doesn't get merged.
6. **API cost guard:** workers must NOT call the real Anthropic API in tests (use the fake clients like in `tests/test_design_ai.py`). Only the QA pass on the live site spends real tokens. 100 agents each running real image designs = ~1000 Haiku calls, and it trips the app's rate limiter.
7. **One server per worker:** if several workers run uvicorn, give each its own port (8765 + n) and its own SQLite DB file, or they'll fight over port 8765.
8. **Don't rename, move or "clean up" files you don't own.** No repo-wide reformatting, no dependency bumps, no "refactor while I'm here".
