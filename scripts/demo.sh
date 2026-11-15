#!/usr/bin/env bash
# Netproof MVP demo script.
#
# Starts the Netproof server and prints the canonical demo walkthrough:
# a 9 V LED circuit with an undersized series resistor -> STOP
# (LED_OVERCURRENT) -> Repair -> PASS, then verify the netlist with ngspice.
#
# Usage:
#   ./scripts/demo.sh
#
# Prerequisites:
#   - ngspice on PATH          (brew install ngspice)
#   - uv                       (https://docs.astral.sh/uv/) or python3 + pip
#   - ANTHROPIC_API_KEY set    (export ANTHROPIC_API_KEY=sk-ant-...)
#
# The server serves the web UI at http://127.0.0.1:8000/

set -euo pipefail

# Always run from the project root, wherever the script is invoked from.
cd "$(dirname "$0")/.."

# --- 1. ngspice check -------------------------------------------------------
if ! command -v ngspice >/dev/null 2>&1; then
    echo "ERROR: ngspice is not on your PATH." >&2
    echo "Netproof verdicts come from real SPICE simulations, so the" >&2
    echo "simulator is required. Install it, then re-run this script:" >&2
    echo "  brew install ngspice        # macOS" >&2
    echo "  sudo apt install ngspice    # Debian/Ubuntu" >&2
    exit 1
fi
echo "ngspice: $(command -v ngspice)"

# --- 2. Anthropic API key check ---------------------------------------------
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "WARNING: ANTHROPIC_API_KEY is not set." >&2
    echo "The server will start, but LLM explanations and repair proposals" >&2
    echo "will be unavailable. The simulator verdicts still work." >&2
    echo "Set it before running for the full demo:" >&2
    echo "  export ANTHROPIC_API_KEY=sk-ant-..." >&2
else
    echo "ANTHROPIC_API_KEY: set"
fi

# --- 3. Install dependencies -------------------------------------------------
if command -v uv >/dev/null 2>&1; then
    echo "Installing dependencies with uv..."
    uv sync
    RUN="uv run"
else
    echo "NOTE: uv not found; using python3 -m pip instead." >&2
    echo "Install uv for the supported setup: https://docs.astral.sh/uv/" >&2
    python3 -m pip install -e . 2>/dev/null \
        || python3 -m pip install fastapi uvicorn pydantic python-multipart sse-starlette anthropic
    RUN="python3 -m"
fi

# --- 4. Start the server ------------------------------------------------------
# backend/ holds the netproof package (see pyproject.toml force-include),
# so it must be on the Python path when not installed as a package.
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}backend"

echo ""
echo "Starting Netproof on http://127.0.0.1:8000/ ..."
echo "Press Ctrl-C to stop."
echo ""
echo "=========================== DEMO STEPS ==========================="
echo ""
echo "Demo: 9 V LED circuit with an undersized series resistor."
echo ""
echo "  1. Open http://127.0.0.1:8000/ in your browser."
echo "  2. Upload a photo of the LED circuit (or submit the bad circuit:"
echo "     9 V source, LED, and a resistor far too small for the LED's"
echo "     current limit)."
echo "  3. STOP: Netproof simulates it and reports LED_OVERCURRENT."
echo "     The verdict comes from ngspice, not from the model."
echo "  4. Repair: accept Netproof's proposed correctly-sized series"
echo "     resistor (sized from the LED's forward voltage and rated"
echo "     current at 9 V)."
echo "  5. PASS: the repaired circuit re-simulates clean. No rules fire."
echo "     The circuit is safe to build."
echo ""
echo "  6. Verify the netlist yourself (optional):"
echo "       - Download netlist.cir from the web UI, then run:"
echo "           ngspice -b netlist.cir"
echo "       - The DC operating-point output should match the verdict"
echo "         shown in the UI."
echo ""
echo "=================================================================="
echo ""

exec $RUN uvicorn netproof.server:app --host 127.0.0.1 --port 8000
