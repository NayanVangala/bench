/**
 * Netproof 3D edit panel — web/3d/edit-panel.js
 *
 * Blueprint rebuild (2026-11-15). Plain JS module (no build step, no CDN,
 * no LLM calls, no ngspice). One same-origin GET /parts for the
 * standard-value catalog (see fetchPartsCatalog); no POST, no LLM, no
 * ngspice. The picker suggests standard values from
 * backend/netproof/parts.py; it never computes a verdict, current,
 * voltage, or power, and it never silently rewrites a non-standard
 * value — Custom… always keeps free-text entry. Shows an edit panel for
 * the component selected in the 3D view.
 *
 * CORRECTION (coordinator, 2026-11-15): the panel had NO dialog
 * semantics before this rebuild — no role="dialog", no focus trap, no
 * Escape handler, no focus return anywhere in the file. Everything in
 * the "Dialog contract" below was added from scratch here.
 *
 * ---------------------------------------------------------------------------
 * Dialog contract (blueprint a11y spec §6)
 * ---------------------------------------------------------------------------
 * - Container: <section id="netproof-edit-panel" role="dialog"
 *   aria-modal="true" aria-labelledby="netproof-edit-panel-title">.
 *   The title announces the component id ("Edit component R1").
 * - Focus IN on open: focus goes to the Close button first (the safe
 *   landing: orientation + exit before the first input).
 * - Focus TRAP while open: Tab from the last focusable control wraps
 *   to Close; Shift+Tab from Close wraps to the last control. A keydown
 *   handler on the panel implements the trap (no `inert` on the page:
 *   the panel lives inside #scene3d, which is inside <main>, so making
 *   the background inert would make the panel itself inert).
 * - Focus RETURN on close: focus returns to the exact control that
 *   opened the panel (the invoking callout, table row, or #scene3d),
 *   captured at showEditPanel time. If the invoker is gone (row or
 *   callout re-rendered), fall back to the part's callout, then #scene3d.
 * - Escape closes from anywhere inside the panel. From inside a
 *   <select>, the first Escape closes the native dropdown and the
 *   second Escape closes the panel (the native dropdown eats the
 *   first one; the panel handler must not fight it).
 * - Tabs (WAI-ARIA, automatic activation): Edit | Measurements |
 *   Findings. Roving tabindex (only the selected tab is 0), arrows
 *   move + select with wrap, Home/End jump, Enter/Space select.
 *   Tab panels are tabindex="0" so a keyboard user can enter and
 *   scroll a long read-only panel. A re-check never auto-switches
 *   tabs; the user stays on the tab they chose.
 * - Save validates, calls onSave, then closes the panel and returns
 *   focus. Re-check / Repair validate and call their handler and leave
 *   the panel open (their results update the Measurements / Findings
 *   panels in place). On validation error, focus stays and the error
 *   is announced in the role="status" line.
 *
 * ---------------------------------------------------------------------------
 * Callout-card chrome (blueprint callouts spec §8)
 * ---------------------------------------------------------------------------
 * The panel is a callout card, not a fixed top-right dialog: same
 * tokens as every other callout (white, 1px border, 8px radius, mono
 * values), border in the part's worst-finding severity colour
 * (STOP #b00020 / WARN #b45309 / PASS #1a7f37, blue #1d4ed8 when the
 * part has no findings). It is positioned inside #scene3d, anchored
 * to the part's callout (right of it, flipped left when there is no
 * room, clamped inside the canvas), with the same thin leader line
 * and dot as a finding callout while it is open. The leader tracks
 * the callout per frame (rAF) so orbit / zoom does not strand it;
 * under prefers-reduced-motion it is drawn once, statically. When
 * #scene3d does not exist (panel opened without the 3D view) it
 * falls back to the old fixed top-right placement.
 *
 * ---------------------------------------------------------------------------
 * Targets, focus, motion
 * ---------------------------------------------------------------------------
 * - Every control is at least 44x44 CSS px (a11y spec §3).
 * - Focus ring is #FFD166 (coordinator decision, 2026-11-15), 3px,
 *   2px offset, plus a dark #0A2A5E keyline so the ring reads on both
 *   the white card and blueprint blue.
 * - Mobile (max-width: 768px): the panel becomes a bottom sheet —
 *   full width, max-height 85vh, drag handle (visual only, never the
 *   sole close control), safe-area padding. Same DOM, same focus
 *   order, same tabs; only positioning changes.
 * - Reduced motion: no transitions, no slide, leader drawn once.
 *
 * Usage (from the page, e.g. web/index.html):
 *
 *   import { showEditPanel, hideEditPanel } from './3d/edit-panel.js';
 *
 *   showEditPanel(component, report, {
 *     onSave:    (edited) => { updateNetlist(edited); postCheck(); },
 *     onRecheck: (edited) => { updateNetlist(edited); postCheck(); },
 *     onRepair:  (edited) => { updateNetlist(edited); postRepair(); },
 *     onClose:   () => { ... }   // optional
 *   });
 *
 * Component shape (see backend/netproof/ir.py, Component):
 *   { id, type, value, nodes: [n1, n2], rating_watts?, color? }
 *   type is one of: resistor | capacitor | led | diode |
 *                   voltage_source | current_source
 *   value is in SI units (ohms, farads, volts, amps).
 *
 * Report shape (see backend/netproof/ir.py, Report) — the latest
 * /check result, or null when no check has run yet:
 *   { verdict, findings: [{ severity, rule_id, component_ids, message }],
 *     node_voltages: { node: volts },
 *     branch_currents: { componentId: amps },
 *     power: { componentId: watts } }
 *
 * Measurements shown here come ONLY from that Report (plus, as a
 * fallback, voltage/current/power fields already on the component
 * object, as the netlist table in index.html carries them). When a
 * value is absent from the Report the panel shows "—", never a
 * guessed or placeholder number.
 *
 * Voltage for a component is the difference of its two node voltages
 * from the Report (V(n1) - V(n2)); the Report carries node voltages,
 * not per-component voltages. The panel labels it "Voltage across
 * component (V(n1) − V(n2))" so it is not confused with the netlist
 * table's first-node voltage (node_voltages[first node]).
 *
 * Callbacks:
 *   onSave(editedComponent)    — required. Save validates the inputs,
 *     builds the edited component object, and hands it to the caller.
 *     The caller (the page) is responsible for updating the netlist
 *     and POSTing /check. This module never POSTs anything itself.
 *     The panel closes after a successful save.
 *   onRecheck(editedComponent) — optional. Re-check button.
 *   onRepair(editedComponent)  — optional. Repair button.
 *   onClose()                  — optional. Called when the panel closes.
 * Buttons whose callback is missing are disabled.
 */

'use strict';

var PANEL_ID = 'netproof-edit-panel';

var COMPONENT_TYPES = [
  'resistor',
  'capacitor',
  'led',
  'diode',
  'voltage_source',
  'current_source',
];

// LED colours known to backend/netproof/parts.py.
var LED_COLORS = ['red', 'green', 'blue', 'white', 'yellow'];

var SEVERITY_ORDER = { STOP: 0, WARN: 1, INFO: 2 };

// Callout-card border: the part's worst-finding severity colour
// (callouts spec §8). Blue is the default when the part has no
// findings; it is never a severity claim.
var SEVERITY_BORDER = {
  STOP: '#b00020',
  WARN: '#b45309',
  PASS: '#1a7f37',
  INFO: '#1d4ed8',
  UNKNOWN: '#1d4ed8',
};

var TAB_NAMES = ['edit', 'measure', 'findings'];

var currentComponent = null; // original component object passed to showEditPanel
var currentReport = null;
var currentCallbacks = {};

// Focus return (a11y spec §6.1): the element that opened the panel,
// captured at showEditPanel time; null when the opener was not a
// focusable element (e.g. a double-click, where focus sits on <body>).
var invoker = null;
var lastComponentId = null; // fallback: focus this part's callout
var activeTab = 'edit';
var selectEscArmed = false; // first Escape from a <select> closes the dropdown only
var leaderSvg = null; // SVG leader line + dot, while the panel is open
var leaderRaf = 0;

// Parts catalog (GET /parts) — fetched lazily on first showEditPanel,
// never at import time (this module never touches the DOM at import).
var catalogCache = null;    // parsed catalog, or null when unavailable
var catalogPromise = null;  // in-flight fetch (dedupes double-open)
var valueCustomMode = false;  // user chose Custom… / typed a custom value
var ratingCustomMode = false; // same, for the power-rating picker
var ledHintVf = null;         // Vf (V) behind the "Use Vf" button, if shown

/* ------------------------------------------------------------------ */
/* Parts catalog (GET /parts)                                         */
/* ------------------------------------------------------------------ */

// Fetch the standard-value catalog once; concurrent callers share the
// one in-flight promise. Any failure (network, non-200, bad JSON)
// resolves to null and clears the promise so a later open retries.
// Never throws to the caller: the panel degrades to free-text entry.
function fetchPartsCatalog() {
  if (catalogCache) return Promise.resolve(catalogCache);
  if (catalogPromise) return catalogPromise;
  if (typeof fetch !== 'function') return Promise.resolve(null);
  catalogPromise = fetch('/parts')
    .then(function (resp) {
      if (!resp || !resp.ok) return null;
      return resp.json();
    })
    .then(function (data) {
      if (!data || typeof data !== 'object' || !data.resistor) return null;
      catalogCache = data;
      return data;
    })
    .catch(function () { return null; })
    .then(function (data) {
      catalogPromise = null;
      return data;
    });
  return catalogPromise;
}

// Nearest catalog value to `current`, by log-distance. Null when the
// list is empty or the current value is not a positive finite number.
function nearestStandard(values, current) {
  if (!Array.isArray(values) || !values.length) return null;
  if (!isFiniteNumber(current) || !(current > 0)) return null;
  var best = values[0];
  var bestD = Math.abs(Math.log(current / values[0]));
  for (var i = 1; i < values.length; i++) {
    var d = Math.abs(Math.log(current / values[i]));
    if (d < bestD) { bestD = d; best = values[i]; }
  }
  return best;
}

// [{ value, label }] entries the picker offers for a type, or null
// when the catalog is unavailable. LED entries are the per-colour Vf
// values (deduplicated: red and yellow share 2.0 V), never resistances.
function valuesForType(catalog, type) {
  if (!catalog) return null;
  var block = catalog[type] || null;
  if (type === 'led') {
    var colors = (catalog.led && catalog.led.colors) || [];
    var seen = {};
    var out = [];
    colors.forEach(function (c) {
      if (c && isFiniteNumber(c.vf_v) && !seen[c.vf_v]) {
        seen[c.vf_v] = true;
        out.push({ value: c.vf_v, label: fmtVf(c.vf_v) });
      }
    });
    out.sort(function (a, b) { return a.value - b.value; });
    return out.length ? out : null;
  }
  if (!block || !Array.isArray(block.values) || !block.values.length) return null;
  return block.values;
}

function labelForValue(entries, value) {
  for (var i = 0; i < entries.length; i++) {
    if (entries[i].value === value) return entries[i].label;
  }
  return null;
}

// "3.0 V" for a Vf value (all catalog Vf values carry one decimal).
function fmtVf(v) {
  return (Math.round(Number(v) * 10) / 10).toFixed(1) + ' V';
}

function ledEntryForColor(color) {
  var colors = catalogCache && catalogCache.led && catalogCache.led.colors;
  if (!Array.isArray(colors)) return null;
  for (var i = 0; i < colors.length; i++) {
    if (colors[i] && colors[i].color === color) return colors[i];
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

// Escape a value for use inside a [data-component-id="..."] selector.
function cssEscapeAttr(s) {
  var v = String(s == null ? '' : s);
  if (typeof CSS !== 'undefined' && CSS && typeof CSS.escape === 'function') return CSS.escape(v);
  return v.replace(/["\\]/g, '\\$&');
}

// Case-insensitive map lookup. Report keys are not one case: ngspice
// lowercases branch-current keys ("r1") while power keeps the
// component id ("R1"), and node names may be either. An exact lookup
// alone showed "—" for current in this panel even though the Report
// carried it (P4).
function lookupCI(map, key) {
  if (!map || key == null) return undefined;
  if (map[key] != null) return map[key];
  var want = String(key).toLowerCase();
  var ks = Object.keys(map);
  for (var i = 0; i < ks.length; i++) {
    if (ks[i].toLowerCase() === want) return map[ks[i]];
  }
  return undefined;
}

// Format a measured number with up to 4 significant digits.
// Returns null when the value is not a finite number (caller shows "—").
function fmt(n) {
  if (!isFiniteNumber(n)) return null;
  if (n === 0) return '0';
  var abs = Math.abs(n);
  if (abs >= 1000 || abs < 0.001) return n.toExponential(3);
  return String(Number(n.toPrecision(4)));
}

function fmtVolts(v) {
  var s = fmt(v);
  return s == null ? null : s + ' V';
}

function fmtAmps(a) {
  var s = fmt(a);
  if (s == null) return null;
  // Also show mA for sub-amp currents; derived from the same number.
  if (Math.abs(a) > 0 && Math.abs(a) < 1) {
    var ma = fmt(a * 1000);
    return ma == null ? s + ' A' : s + ' A (' + ma + ' mA)';
  }
  return s + ' A';
}

function fmtWatts(w) {
  var s = fmt(w);
  if (s == null) return null;
  if (Math.abs(w) > 0 && Math.abs(w) < 1) {
    var mw = fmt(w * 1000);
    return mw == null ? s + ' W' : s + ' W (' + mw + ' mW)';
  }
  return s + ' W';
}

function el(id) {
  return document.getElementById(id);
}

function setStatus(msg, isError) {
  var s = el(PANEL_ID + '-status');
  if (!s) return;
  s.textContent = msg || '';
  s.className = 'ep-status' + (isError ? ' ep-error' : '');
}

/* ------------------------------------------------------------------ */
/* Measurements + findings from the Report                             */
/* ------------------------------------------------------------------ */

// Voltage across the component: V(nodes[0]) - V(nodes[1]) from the
// Report's node_voltages. Null when either node voltage is missing.
function componentVoltage(component, report) {
  if (report && report.node_voltages && component.nodes) {
    var v1 = lookupCI(report.node_voltages, component.nodes[0]);
    var v2 = lookupCI(report.node_voltages, component.nodes[1]);
    if (isFiniteNumber(v1) && isFiniteNumber(v2)) return v1 - v2;
  }
  // Fallback: a voltage already carried on the component object
  // (the netlist table in index.html uses node_voltage / voltage).
  if (isFiniteNumber(component.node_voltage)) return component.node_voltage;
  if (isFiniteNumber(component.voltage)) return component.voltage;
  return null;
}

function componentCurrent(component, report) {
  if (report && report.branch_currents) {
    var i = lookupCI(report.branch_currents, component.id);
    if (isFiniteNumber(i)) return i;
  }
  if (isFiniteNumber(component.current)) return component.current;
  return null;
}

function componentPower(component, report) {
  if (report && report.power) {
    var p = lookupCI(report.power, component.id);
    if (isFiniteNumber(p)) return p;
  }
  if (isFiniteNumber(component.power)) return component.power;
  return null;
}

// Findings that mention this component, STOP first, then WARN, INFO.
// Accepts the Report shape (component_ids array) and, defensively,
// findings that carry a single component / id / node field.
function findingsFor(component, report) {
  if (!report || !Array.isArray(report.findings)) return [];
  var id = component.id;
  var list = report.findings.filter(function (f) {
    if (!f || typeof f !== 'object') return false;
    if (Array.isArray(f.component_ids)) return f.component_ids.indexOf(id) !== -1;
    if (typeof f.component_ids === 'string') return f.component_ids === id;
    return f.component === id || f.id === id || f.node === id;
  });
  return list.slice().sort(function (a, b) {
    var ra = SEVERITY_ORDER[String(a.severity || '').toUpperCase()];
    var rb = SEVERITY_ORDER[String(b.severity || '').toUpperCase()];
    return (ra == null ? 99 : ra) - (rb == null ? 99 : rb);
  });
}

// Worst severity in a findings list, for the callout-card border
// (STOP > WARN > INFO). 'INFO' when the list is empty: blue default,
// never a severity claim.
function worstSeverity(list) {
  var worst = null;
  var worstRank = 99;
  (list || []).forEach(function (f) {
    var sev = String((f && (f.severity || f.level)) || '').toUpperCase();
    var rank = SEVERITY_ORDER[sev];
    if (rank != null && rank < worstRank) { worstRank = rank; worst = sev; }
  });
  return worst || 'INFO';
}

/* ------------------------------------------------------------------ */
/* Panel DOM: dialog, tabs, focus trap, callout-card anchoring         */
/* ------------------------------------------------------------------ */

// Focusable controls inside the open panel, in DOM order: Close,
// the selected tab (roving tabindex), the active tabpanel, then the
// active panel's controls. Hidden tabpanels (display:none) have no
// offsetParent and are excluded; disabled buttons are excluded.
function focusableElements() {
  var panel = el(PANEL_ID);
  if (!panel) return [];
  var nodes = panel.querySelectorAll(
    'button:not([disabled]), select:not([disabled]), input:not([disabled]), [tabindex="0"]');
  var out = [];
  for (var i = 0; i < nodes.length; i++) {
    var n = nodes[i];
    if (n.offsetParent !== null || n === document.activeElement) out.push(n);
  }
  return out;
}

// Switch tabs (automatic activation): update aria-selected, roving
// tabindex, and which tabpanel is shown. Focus the tab when asked
// (arrow-key navigation); clicks focus it natively.
function selectTab(name, focusTab) {
  if (TAB_NAMES.indexOf(name) === -1) name = 'edit';
  activeTab = name;
  TAB_NAMES.forEach(function (n) {
    var tab = el(PANEL_ID + '-tab-' + n);
    var tabpanel = el(PANEL_ID + '-panel-' + n);
    var selected = n === name;
    if (tab) {
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      tab.tabIndex = selected ? 0 : -1;
    }
    if (tabpanel) {
      if (selected) tabpanel.removeAttribute('hidden');
      else tabpanel.setAttribute('hidden', '');
    }
  });
  if (focusTab) {
    var tab = el(PANEL_ID + '-tab-' + name);
    if (tab) tab.focus();
  }
}

// Panel keydown: tablist arrows/Home/End, focus trap, Escape.
// The trap wraps Tab from the last control to Close and Shift+Tab
// from Close to the last control; focus can never leave the dialog
// by Tab while it is open.
function onPanelKeyDown(e) {
  if (!e || !e.key) return;
  var tabEl = (e.target && e.target.closest) ? e.target.closest('[role="tab"]') : null;
  if (tabEl && (e.key === 'ArrowRight' || e.key === 'ArrowDown' ||
      e.key === 'ArrowLeft' || e.key === 'ArrowUp' ||
      e.key === 'Home' || e.key === 'End')) {
    e.preventDefault();
    var i = TAB_NAMES.indexOf(activeTab);
    var next;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = TAB_NAMES[(i + 1) % TAB_NAMES.length];
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = TAB_NAMES[(i - 1 + TAB_NAMES.length) % TAB_NAMES.length];
    else if (e.key === 'Home') next = TAB_NAMES[0];
    else next = TAB_NAMES[TAB_NAMES.length - 1];
    selectTab(next, true);
    return;
  }
  if ((e.key === 'Enter' || e.key === ' ') && tabEl) {
    // Enter/Space on a focused tab selects it (redundant under
    // automatic activation, but harmless).
    e.preventDefault();
    selectTab(tabEl.getAttribute('data-tab'), true);
    selectEscArmed = false;
    return;
  }
  if (e.key === 'Escape') {
    // A native <select> dropdown eats the first Escape to close the
    // dropdown; the drawer closes on the second Escape from a select.
    // Anywhere else, Escape closes immediately.
    if (e.target && e.target.tagName === 'SELECT' && !selectEscArmed) {
      selectEscArmed = true;
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    hideEditPanel();
    return;
  }
  if (e.key === 'Tab') {
    var focusables = focusableElements();
    if (!focusables.length) { e.preventDefault(); return; }
    var first = focusables[0];
    var last = focusables[focusables.length - 1];
    var idx = focusables.indexOf(e.target);
    if (idx === -1) {
      e.preventDefault();
      (e.shiftKey ? last : first).focus();
    } else if (e.shiftKey && idx === 0) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && idx === focusables.length - 1) {
      e.preventDefault();
      first.focus();
    }
    selectEscArmed = false;
    return;
  }
  selectEscArmed = false;
}

// Return focus to the exact control that opened the panel. Fallbacks,
// in order: the part's callout button (when the invoker was re-rendered
// away), then #scene3d. Never steals focus when the panel never opened.
function focusInvoker() {
  var target = null;
  if (invoker && invoker.isConnected && !invoker.disabled &&
      typeof invoker.focus === 'function' &&
      (invoker.offsetParent !== null || invoker.id === 'scene3d')) {
    target = invoker;
  }
  if (!target && lastComponentId) {
    var callout = document.querySelector(
      '.netproof-callout[data-component-id="' + cssEscapeAttr(lastComponentId) + '"]');
    if (callout && callout.offsetParent !== null) target = callout;
  }
  if (!target) target = document.getElementById('scene3d');
  if (target && typeof target.focus === 'function') {
    try { target.focus(); } catch (e) { /* focus is best-effort */ }
  }
}

// Remove the leader line + dot and stop its tracking loop.
function clearLeader() {
  if (leaderRaf) {
    try { cancelAnimationFrame(leaderRaf); } catch (e) { /* already gone */ }
    leaderRaf = 0;
  }
  if (leaderSvg && leaderSvg.parentNode) leaderSvg.parentNode.removeChild(leaderSvg);
  leaderSvg = null;
}

// Draw the thin leader line + dot from the panel edge to the part's
// callout anchor (callouts spec §8). Tracks the callout per frame so
// orbit / zoom does not strand it; under reduced motion it is drawn
// once, statically.
function drawLeader(anchor) {
  clearLeader();
  var panel = el(PANEL_ID);
  if (!panel || !anchor) return;
  var host = panel.parentElement;
  if (!host || host.id !== 'scene3d') return;
  var svgNS = 'http://www.w3.org/2000/svg';
  var svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('class', 'ep-leader');
  svg.setAttribute('aria-hidden', 'true');
  svg.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;z-index:5;overflow:visible;';
  var line = document.createElementNS(svgNS, 'line');
  var dot = document.createElementNS(svgNS, 'circle');
  var color = panel.style.borderColor || '#1d4ed8';
  line.setAttribute('stroke', color);
  line.setAttribute('stroke-width', '1');
  dot.setAttribute('r', '3');
  dot.setAttribute('fill', color);
  dot.setAttribute('stroke', '#fff');
  dot.setAttribute('stroke-width', '1');
  svg.appendChild(line);
  svg.appendChild(dot);
  host.appendChild(svg);
  leaderSvg = svg;
  var reduceMotion = false;
  try {
    reduceMotion = !!(window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch (e) { /* assume motion is fine */ }
  function update() {
    if (!leaderSvg || !el(PANEL_ID) || el(PANEL_ID).style.display === 'none') return;
    var hostRect = host.getBoundingClientRect();
    var a = anchor.getBoundingClientRect();
    var ax = a.left - hostRect.left + a.width / 2;
    var ay = a.top - hostRect.top + a.height / 2;
    var pr = el(PANEL_ID).getBoundingClientRect();
    var px = pr.left - hostRect.left;
    var py = pr.top - hostRect.top;
    var cx = Math.max(px, Math.min(ax, px + pr.width));
    var cy = Math.max(py, Math.min(ay, py + pr.height));
    line.setAttribute('x1', String(cx));
    line.setAttribute('y1', String(cy));
    line.setAttribute('x2', String(ax));
    line.setAttribute('y2', String(ay));
    dot.setAttribute('cx', String(ax));
    dot.setAttribute('cy', String(ay));
  }
  update();
  if (!reduceMotion) {
    (function tick() {
      if (!leaderSvg) return;
      update();
      leaderRaf = requestAnimationFrame(tick);
    })();
  }
}

// Anchor the callout card inside #scene3d: right of the part's
// callout, flipped left when there is no room, clamped inside the
// canvas and below the verdict callout's corner. Below 768px the
// bottom-sheet stylesheet owns placement, so no inline offsets are
// set. Without #scene3d, the fixed top-right fallback applies.
function positionPanel() {
  var panel = el(PANEL_ID);
  if (!panel) return;
  var host = panel.parentElement;
  var inScene = !!host && host.id === 'scene3d';
  panel.classList.toggle('ep-in-scene', inScene);
  if (!inScene) {
    panel.style.position = 'fixed';
    panel.style.top = '16px';
    panel.style.right = '16px';
    panel.style.left = 'auto';
    panel.style.bottom = 'auto';
    clearLeader();
    return;
  }
  panel.style.position = 'absolute';
  var narrow = false;
  try {
    narrow = !!(window.matchMedia && window.matchMedia('(max-width: 768px)').matches);
  } catch (e) { /* desktop layout */ }
  if (narrow) {
    panel.style.top = '';
    panel.style.right = '';
    panel.style.left = '';
    panel.style.bottom = '';
    clearLeader();
    return;
  }
  var anchor = null;
  if (currentComponent) {
    anchor = host.querySelector(
      '.netproof-callout[data-component-id="' + cssEscapeAttr(currentComponent.id) + '"]');
    if (anchor && anchor.offsetParent === null) anchor = null; // not projected
  }
  var hostRect = host.getBoundingClientRect();
  var pw = panel.offsetWidth;
  var ph = panel.offsetHeight;
  var x;
  var y;
  if (anchor) {
    var r = anchor.getBoundingClientRect();
    var ax = r.left - hostRect.left;
    var ay = r.top - hostRect.top;
    x = ax + r.width + 12;
    y = ay;
    if (x + pw > hostRect.width - 12) x = ax - pw - 12;
    if (x < 12) x = 12;
  } else {
    x = hostRect.width - pw - 12;
    y = 12;
    anchor = null;
  }
  if (y + ph > hostRect.height - 12) y = Math.max(12, hostRect.height - ph - 12);
  if (y < 12) y = 12;
  panel.style.left = Math.round(x) + 'px';
  panel.style.top = Math.round(y) + 'px';
  panel.style.right = 'auto';
  panel.style.bottom = 'auto';
  drawLeader(anchor);
}

function buildPanel() {
  wireTypeChange();
  var panel = el(PANEL_ID);
  if (panel) return panel;

  panel = document.createElement('section');
  panel.id = PANEL_ID;
  panel.className = 'bp-drawer';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-labelledby', PANEL_ID + '-title');
  panel.setAttribute('aria-label', 'Edit selected component');
  panel.innerHTML = [
    '<button type="button" class="ep-close" data-action="close" aria-label="Close edit panel">✕</button>',
    '<h2 id="' + PANEL_ID + '-title">Edit component</h2>',
    '<p class="ep-sub" id="' + PANEL_ID + '-sub"></p>',
    '<div><label>ID</label><div id="' + PANEL_ID + '-id"></div></div>',
    '<div class="ep-tabs" role="tablist" aria-label="Edit component panels">',
    '<button type="button" role="tab" class="ep-tab" data-tab="edit" id="' + PANEL_ID + '-tab-edit" aria-selected="true" aria-controls="' + PANEL_ID + '-panel-edit" tabindex="0">Edit</button>',
    '<button type="button" role="tab" class="ep-tab" data-tab="measure" id="' + PANEL_ID + '-tab-measure" aria-selected="false" aria-controls="' + PANEL_ID + '-panel-measure" tabindex="-1">Measurements</button>',
    '<button type="button" role="tab" class="ep-tab" data-tab="findings" id="' + PANEL_ID + '-tab-findings" aria-selected="false" aria-controls="' + PANEL_ID + '-panel-findings" tabindex="-1">Findings</button>',
    '</div>',
    '<div role="tabpanel" id="' + PANEL_ID + '-panel-edit" aria-labelledby="' + PANEL_ID + '-tab-edit" tabindex="0">',
    '<div><label for="' + PANEL_ID + '-type">Type</label>',
    '<select id="' + PANEL_ID + '-type">',
    COMPONENT_TYPES.map(function (t) {
      return '<option value="' + t + '">' + t + '</option>';
    }).join(''),
    '</select></div>',
    '<div><label for="' + PANEL_ID + '-value">Value <span class="ep-hint" id="' + PANEL_ID + '-unit"></span></label>',
    '<select id="' + PANEL_ID + '-value-pick" style="display:none" aria-label="Standard value"></select>',
    '<input id="' + PANEL_ID + '-value" type="number" step="any" inputmode="decimal">',
    '<div class="ep-hint" id="' + PANEL_ID + '-value-hint"></div></div>',
    '<div><label>Nodes</label><div class="ep-nodes">',
    '<div><input id="' + PANEL_ID + '-node0" type="text" aria-label="First node" placeholder="node 1"></div>',
    '<div><input id="' + PANEL_ID + '-node1" type="text" aria-label="Second node" placeholder="node 2"></div>',
    '</div></div>',
    '<div><label for="' + PANEL_ID + '-rating">Power rating (W) <span class="ep-hint">resistors default 0.25 W</span></label>',
    '<select id="' + PANEL_ID + '-rating-pick" style="display:none" aria-label="Standard power rating"></select>',
    '<input id="' + PANEL_ID + '-rating" type="number" step="any" min="0" inputmode="decimal" placeholder="0.25"></div>',
    '<div><label for="' + PANEL_ID + '-color">Colour <span class="ep-hint">LEDs only</span></label>',
    '<select id="' + PANEL_ID + '-color">',
    '<option value="">—</option>',
    LED_COLORS.map(function (c) {
      return '<option value="' + c + '">' + c + '</option>';
    }).join(''),
    '</select>',
    '<div class="ep-led-hint" id="' + PANEL_ID + '-led-hint" style="display:none">' +
    '<span class="ep-hint" id="' + PANEL_ID + '-led-hint-text"></span>' +
    '<button type="button" class="secondary" data-action="use-vf" id="' + PANEL_ID + '-led-use-vf">Use Vf</button>' +
    '</div></div>',
    '<div class="ep-actions">',
    '<button type="button" data-action="save">Save</button>',
    '<button type="button" class="secondary" data-action="recheck">Re-check</button>',
    '<button type="button" class="secondary" data-action="repair">Repair</button>',
    '</div>',
    '<p class="ep-status" id="' + PANEL_ID + '-status" role="status"></p>',
    '</div>',
    '<div role="tabpanel" id="' + PANEL_ID + '-panel-measure" aria-labelledby="' + PANEL_ID + '-tab-measure" tabindex="0" hidden>',
    '<h3>Latest simulation</h3>',
    '<div id="' + PANEL_ID + '-measure"></div>',
    '</div>',
    '<div role="tabpanel" id="' + PANEL_ID + '-panel-findings" aria-labelledby="' + PANEL_ID + '-tab-findings" tabindex="0" hidden>',
    '<h3>Findings for this component</h3>',
    '<div id="' + PANEL_ID + '-findings"></div>',
    '</div>',
  ].join('');

  panel.addEventListener('click', function (e) {
    var tab = (e.target && e.target.closest) ? e.target.closest('[role="tab"]') : null;
    if (tab) { selectTab(tab.getAttribute('data-tab'), true); return; }
    var btn = (e.target && e.target.closest) ? e.target.closest('[data-action]') : null;
    if (!btn) return;
    var action = btn.getAttribute('data-action');
    if (action === 'close') hideEditPanel();
    else if (action === 'save') onSaveClick();
    else if (action === 'recheck') onButtonClick('onRecheck');
    else if (action === 'repair') onButtonClick('onRepair');
    else if (action === 'use-vf') onUseVfClick();
  });

  panel.addEventListener('keydown', onPanelKeyDown);

  // Keep the anchored card inside the canvas when the viewport
  // changes (a shrink below 768px hands placement to the sheet CSS).
  window.addEventListener('resize', function () {
    var p = el(PANEL_ID);
    if (p && p.style.display !== 'none' && currentComponent) positionPanel();
  });

  // The callout card lives inside #scene3d so it moves with the
  // canvas on scroll and never covers the page; without a 3D view it
  // falls back to document.body (fixed top-right, as before).
  var host = document.getElementById('scene3d') || document.body;
  if (host !== document.body) {
    try {
      if (window.getComputedStyle(host).position === 'static') host.style.position = 'relative';
    } catch (e) { /* labels misplace without it; panel still works */ }
  }
  host.appendChild(panel);
  return panel;
}

/* ------------------------------------------------------------------ */
/* Rendering                                                           */
/* ------------------------------------------------------------------ */

function valueUnit(type) {
  switch (type) {
    case 'resistor': return '(Ω)';
    case 'capacitor': return '(F)';
    case 'led':
    case 'diode': return '(V forward voltage)';
    case 'voltage_source': return '(V)';
    case 'current_source': return '(A)';
    default: return '';
  }
}

function renderMeasurements(component, report) {
  var box = el(PANEL_ID + '-measure');
  if (!box) return;
  if (!report) {
    box.innerHTML = '<p class="ep-empty">No report yet. Run a check first; ' +
      'voltage, current and power appear here after /check returns.</p>';
    return;
  }
  var v = componentVoltage(component, report);
  var i = componentCurrent(component, report);
  var p = componentPower(component, report);
  var fv = fmtVolts(v);
  var fi = fmtAmps(i);
  var fp = fmtWatts(p);
  // Label the voltage explicitly as the voltage ACROSS this component,
  // V(n1) - V(n2), with the component's actual node names, so it is
  // never read as the netlist table's first-node voltage
  // (node_voltages[first node]). The computed value is unchanged:
  // componentVoltage() still returns V(nodes[0]) - V(nodes[1]).
  var nodes = Array.isArray(component.nodes) ? component.nodes : [];
  var n1 = nodes[0] != null ? String(nodes[0]) : 'n1';
  var n2 = nodes[1] != null ? String(nodes[1]) : 'n2';
  box.innerHTML = '<table class="ep-measure"><tbody>' +
    '<tr><td>Voltage across component (V(' + escapeHtml(n1) + ') \u2212 V(' + escapeHtml(n2) + '))</td><td>' + (fv == null ? '—' : escapeHtml(fv)) + '</td></tr>' +
    '<tr><td>Current</td><td>' + (fi == null ? '—' : escapeHtml(fi)) + '</td></tr>' +
    '<tr><td>Power</td><td>' + (fp == null ? '—' : escapeHtml(fp)) + '</td></tr>' +
    '</tbody></table>' +
    '<p class="ep-hint">From the latest /check report' +
    (report.verdict ? ' (verdict: ' + escapeHtml(String(report.verdict)) + ')' : '') +
    '. Voltage across the component is V(n1) \u2212 V(n2) (first-node voltage minus second-node voltage), ' +
    'not the netlist table\u2019s first-node voltage. ' +
    '“—” means the report did not include that value for this component.</p>';
}

function renderFindings(component, report) {
  var box = el(PANEL_ID + '-findings');
  if (!box) return;
  if (!report) {
    box.innerHTML = '<p class="ep-empty">No findings yet. Run a check first.</p>';
    return;
  }
  var list = findingsFor(component, report);
  if (!list.length) {
    box.innerHTML = '<p class="ep-empty">No findings for this component.</p>';
    return;
  }
  box.innerHTML = '<ul class="ep-findings">' + list.map(function (f) {
    var sev = String(f.severity || f.level || '').toUpperCase() || 'INFO';
    var rule = f.rule_id || f.rule || '';
    var msg = f.message || f.text || f.detail || '';
    return '<li><span class="ep-sev ep-sev-' + escapeHtml(sev.toLowerCase()) + '">' +
      escapeHtml(sev) + '</span>' +
      (rule ? ' <strong>' + escapeHtml(rule) + '</strong>' : '') +
      (msg ? ' ' + escapeHtml(msg) : '') + '</li>';
  }).join('') + '</ul>';
}

function valueUnitSymbol(type) {
  switch (type) {
    case 'resistor': return '\u03a9';
    case 'capacitor': return 'F';
    case 'led':
    case 'diode':
    case 'voltage_source': return 'V';
    case 'current_source': return 'A';
    default: return '';
  }
}

// Fill the colour <select> from the catalog (fallback: LED_COLORS
// when the catalog is unavailable). Preserves the current selection.
function populateColorSelect() {
  var sel = el(PANEL_ID + '-color');
  if (!sel) return;
  var colors = catalogCache && catalogCache.led && catalogCache.led.colors;
  var names = colors
    ? colors.map(function (c) { return c.color; })
    : LED_COLORS;
  var current = sel.value;
  sel.innerHTML = '<option value="">—</option>' + names.map(function (c) {
    return '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + '</option>';
  }).join('');
  sel.value = names.indexOf(current) !== -1 ? current : '';
}

// Fill the value picker for the current type and preselect the entry
// nearest (log-distance) to the number input's value. The number input
// stays the source of truth for readEditedComponent(): a non-standard
// value is preserved (nearest shown + hint, value untouched), and a
// value orders of magnitude from anything standard for the new type
// falls back to Custom… with the number preserved, never rewritten.
function populateValuePicker() {
  var pick = el(PANEL_ID + '-value-pick');
  var input = el(PANEL_ID + '-value');
  var hint = el(PANEL_ID + '-value-hint');
  if (!pick || !input) return;
  var type = el(PANEL_ID + '-type').value;
  var entries = valuesForType(catalogCache, type);
  if (!entries) {
    // Catalog unavailable: free-text only, exactly as before.
    pick.style.display = 'none';
    input.style.display = '';
    if (hint) hint.textContent = '';
    return;
  }
  pick.innerHTML = '';
  entries.forEach(function (e) {
    var opt = document.createElement('option');
    opt.value = String(e.value);
    opt.textContent = e.label;
    pick.appendChild(opt);
  });
  var customOpt = document.createElement('option');
  customOpt.value = 'custom';
  customOpt.textContent = 'Custom\u2026';
  pick.appendChild(customOpt);
  pick.style.display = '';

  var current = Number(input.value);
  var haveCurrent = input.value.trim() !== '' && isFiniteNumber(current);
  if (valueCustomMode) {
    pick.value = 'custom';
    input.style.display = '';
    if (hint) hint.textContent = '';
    return;
  }
  if (!haveCurrent) {
    pick.selectedIndex = 0;
    input.style.display = 'none';
    if (hint) hint.textContent = '';
    return;
  }
  var values = entries.map(function (e) { return e.value; });
  var nearest = nearestStandard(values, current);
  var exact = entries.some(function (e) { return e.value === current; });
  if (nearest != null && Math.abs(Math.log(current / nearest)) > Math.log(1000)) {
    pick.value = 'custom';
    valueCustomMode = true;
    input.style.display = '';
    if (hint) hint.textContent = '';
    return;
  }
  pick.value = String(nearest);
  input.style.display = 'none';
  if (hint) {
    if (!exact && nearest != null) {
      var series = (type === 'resistor' || type === 'capacitor')
        ? 'standard E12 value' : 'standard value';
      hint.textContent = 'Current value ' + fmt(current) + ' ' +
        valueUnitSymbol(type) + ' is not a ' + series +
        ' \u2014 nearest is ' + labelForValue(entries, nearest) + '.';
    } else {
      hint.textContent = '';
    }
  }
}

// Fill the rating picker from catalog.resistor.ratings_w. A blank
// rating stays blank (blank falls back to the component's original
// rating / backend 0.25 W default); a non-standard typed rating
// keeps Custom… with the number preserved.
function populateRatingPicker() {
  var pick = el(PANEL_ID + '-rating-pick');
  var input = el(PANEL_ID + '-rating');
  if (!pick || !input) return;
  var ratings = catalogCache && catalogCache.resistor &&
    catalogCache.resistor.ratings_w;
  if (!Array.isArray(ratings) || !ratings.length) {
    pick.style.display = 'none';
    input.style.display = '';
    return;
  }
  pick.innerHTML = '';
  ratings.forEach(function (r) {
    var opt = document.createElement('option');
    opt.value = String(r);
    opt.textContent = String(r) + ' W';
    pick.appendChild(opt);
  });
  var customOpt = document.createElement('option');
  customOpt.value = 'custom';
  customOpt.textContent = 'Custom\u2026';
  pick.appendChild(customOpt);
  pick.style.display = '';

  var current = Number(input.value);
  var haveCurrent = input.value.trim() !== '' && isFiniteNumber(current);
  if (ratingCustomMode || (haveCurrent && ratings.indexOf(current) === -1)) {
    if (haveCurrent && ratings.indexOf(current) === -1) ratingCustomMode = true;
    pick.value = 'custom';
    input.style.display = '';
    return;
  }
  if (haveCurrent) {
    pick.value = String(current);
    input.style.display = 'none';
    return;
  }
  var def = catalogCache.resistor.default_rating_w;
  pick.value = String(isFiniteNumber(def) ? def : 0.25);
  input.style.display = 'none';
}

// Show the LED Vf hint for `color`, or hide it. `mismatchOnly`
// (load path): only show when the value disagrees with the colour's
// Vf by more than rounding. Colour-change path passes false: always
// show the typical Vf. Never overwrites the value; the "Use Vf"
// button applies it, and only when the user clicks it.
function refreshLedHint(color, mismatchOnly) {
  var box = el(PANEL_ID + '-led-hint');
  var text = el(PANEL_ID + '-led-hint-text');
  var btn = el(PANEL_ID + '-led-use-vf');
  if (!box || !text || !btn) return;
  var type = el(PANEL_ID + '-type') ? el(PANEL_ID + '-type').value : '';
  var entry = ledEntryForColor(color);
  ledHintVf = null;
  if (type !== 'led' || !entry || !color) {
    box.style.display = 'none';
    return;
  }
  var vfText = fmtVf(entry.vf_v);
  var current = Number(el(PANEL_ID + '-value').value);
  var matches = isFiniteNumber(current) && Math.abs(current - entry.vf_v) <= 0.05;
  if (mismatchOnly && matches) {
    box.style.display = 'none';
    return;
  }
  var maxMa = fmt(entry.if_max_a * 1000);
  var warnMa = fmt(entry.if_warn_a * 1000);
  if (mismatchOnly) {
    text.textContent = 'Value ' + fmt(current) + ' V does not match ' + color +
      ' Vf ' + vfText + ' (max ' + maxMa + ' mA, warn at ' + warnMa + ' mA).';
  } else {
    text.textContent = 'Typical Vf for ' + color + ': ' + vfText +
      ' (max ' + maxMa + ' mA, warn at ' + warnMa + ' mA).';
  }
  btn.textContent = 'Use ' + vfText;
  ledHintVf = entry.vf_v;
  box.style.display = '';
}

function onUseVfClick() {
  if (!isFiniteNumber(ledHintVf)) return;
  var input = el(PANEL_ID + '-value');
  if (!input) return;
  valueCustomMode = false;
  input.value = String(ledHintVf);
  populateValuePicker();
  var box = el(PANEL_ID + '-led-hint');
  if (box) box.style.display = 'none';
  ledHintVf = null;
}

function refreshPickers() {
  populateColorSelect();
  populateValuePicker();
  populateRatingPicker();
  var color = el(PANEL_ID + '-color') ? el(PANEL_ID + '-color').value : '';
  refreshLedHint(color, true);
}

function renderPanel() {
  var c = currentComponent;
  if (!c) return;
  var panel = buildPanel();
  panel.style.display = 'block';

  // Dialog title announces the component id on open.
  var title = el(PANEL_ID + '-title');
  if (title) title.textContent = 'Edit component ' + (c.id || '');
  el(PANEL_ID + '-sub').textContent = 'Editing the selected 3D component. ' +
    'Save hands the edited component back to the page, which POSTs /check.';
  el(PANEL_ID + '-id').textContent = c.id || '';
  el(PANEL_ID + '-type').value = COMPONENT_TYPES.indexOf(c.type) !== -1 ? c.type : '';
  el(PANEL_ID + '-unit').textContent = valueUnit(c.type);
  el(PANEL_ID + '-value').value = c.value != null ? String(c.value) : '';
  el(PANEL_ID + '-node0').value = Array.isArray(c.nodes) && c.nodes[0] != null ? String(c.nodes[0]) : '';
  el(PANEL_ID + '-node1').value = Array.isArray(c.nodes) && c.nodes[1] != null ? String(c.nodes[1]) : '';
  el(PANEL_ID + '-rating').value = c.rating_watts != null ? String(c.rating_watts) : '';
  el(PANEL_ID + '-color').value = c.color != null ? String(c.color).toLowerCase() : '';

  // Colour only applies to LEDs/diodes; rating is meaningful mainly
  // for resistors. Keep both inputs visible (they are real Component
  // fields) but disable colour for non-LED/diode types.
  var type = el(PANEL_ID + '-type').value;
  el(PANEL_ID + '-color').disabled = !(type === 'led' || type === 'diode');

  // Fresh open: no custom value typed yet. Populate the pickers from
  // whatever catalog is cached (or fall back to free-text inputs when
  // it is not), then upgrade in place when the fetch lands.
  valueCustomMode = false;
  ratingCustomMode = false;
  selectEscArmed = false;
  refreshPickers();
  var openedId = c.id;
  fetchPartsCatalog().then(function (catalog) {
    if (!currentComponent || currentComponent.id !== openedId) return;
    if (catalog) {
      refreshPickers();
    } else {
      setStatus('Standard values unavailable \u2014 enter a custom value.');
    }
  });

  renderMeasurements(c, currentReport);
  renderFindings(c, currentReport);

  // Callout-card border: the part's worst-finding severity colour.
  var findings = findingsFor(c, currentReport);
  panel.style.borderColor = SEVERITY_BORDER[worstSeverity(findings)] || '#1d4ed8';
  // Findings tab badge: text count of STOP findings, never a colour
  // dot alone.
  var stops = findings.filter(function (f) {
    return String((f && (f.severity || f.level)) || '').toUpperCase() === 'STOP';
  }).length;
  var findingsTab = el(PANEL_ID + '-tab-findings');
  if (findingsTab) findingsTab.textContent = stops > 0 ? 'Findings (' + stops + ')' : 'Findings';

  setStatus('');

  // Re-check / Repair need a handler from the page; disable otherwise.
  var recheckBtn = panel.querySelector('[data-action="recheck"]');
  var repairBtn = panel.querySelector('[data-action="repair"]');
  if (recheckBtn) recheckBtn.disabled = typeof currentCallbacks.onRecheck !== 'function';
  if (repairBtn) repairBtn.disabled = typeof currentCallbacks.onRepair !== 'function';
  var saveBtn = panel.querySelector('[data-action="save"]');
  if (saveBtn) saveBtn.disabled = typeof currentCallbacks.onSave !== 'function';

  // Fresh open always lands on the Edit tab; a re-check never
  // auto-switches tabs while the panel is already open.
  selectTab('edit', false);

  // Anchor the card, then focus IN: Close first (orientation + exit
  // before the first input), per a11y spec §6.1.
  positionPanel();
  var closeBtn = panel.querySelector('[data-action="close"]');
  if (closeBtn) closeBtn.focus();
}

/* ------------------------------------------------------------------ */
/* Editing                                                             */
/* ------------------------------------------------------------------ */

// Read the panel inputs and build the edited component object.
// Returns { component } on success or { error } on invalid input.
// Fields the panel does not edit are carried over unchanged, so the
// object stays a valid Component (see backend/netproof/ir.py).
function readEditedComponent() {
  var c = currentComponent;
  if (!c) return { error: 'No component is selected.' };

  var type = el(PANEL_ID + '-type').value;
  var valueRaw = el(PANEL_ID + '-value').value.trim();
  var node0 = el(PANEL_ID + '-node0').value.trim();
  var node1 = el(PANEL_ID + '-node1').value.trim();
  var ratingRaw = el(PANEL_ID + '-rating').value.trim();
  var colorRaw = el(PANEL_ID + '-color').value.trim();

  if (COMPONENT_TYPES.indexOf(type) === -1) {
    return { error: 'Pick a component type.' };
  }
  if (valueRaw === '' || Number.isNaN(Number(valueRaw))) {
    return { error: 'Value must be a number (SI units: ohms, farads, volts, amps).' };
  }
  var value = Number(valueRaw);
  if (!Number.isFinite(value)) return { error: 'Value must be a finite number.' };
  // Matches the backend rule (ir.py): resistor / capacitor / LED /
  // diode values must be > 0. Sources may be signed.
  if ((type === 'resistor' || type === 'capacitor' || type === 'led' || type === 'diode') && !(value > 0)) {
    return { error: 'A ' + type + ' value must be greater than 0.' };
  }
  if (!node0 || !node1) return { error: 'Both nodes are required.' };
  if (node0 === node1) return { error: 'The two nodes must be different.' };

  var rating = null;
  if (ratingRaw !== '') {
    rating = Number(ratingRaw);
    if (Number.isNaN(rating) || !Number.isFinite(rating) || !(rating > 0)) {
      return { error: 'Power rating must be greater than 0 W, or left blank.' };
    }
  }

  var edited = {
    id: c.id,
    type: type,
    value: value,
    nodes: [node0, node1],
  };
  // rating_watts: keep the panel value when given; otherwise carry
  // over the original (the backend defaults resistors to 0.25 W).
  if (rating != null) edited.rating_watts = rating;
  else if (c.rating_watts != null) edited.rating_watts = c.rating_watts;
  // color: only meaningful for LEDs/diodes.
  if (type === 'led' || type === 'diode') {
    if (colorRaw) edited.color = colorRaw;
    else if (c.color != null) edited.color = c.color;
  } else if (c.color != null) {
    edited.color = c.color;
  }
  return { component: edited };
}

function onSaveClick() {
  var result = readEditedComponent();
  if (result.error) { setStatus(result.error, true); return; }
  if (typeof currentCallbacks.onSave !== 'function') {
    setStatus('No save handler is wired up.', true);
    return;
  }
  setStatus('');
  currentCallbacks.onSave(result.component);
  // Save closes the dialog (a11y spec §5.4) and returns focus to the
  // invoking callout / row / #scene3d. Re-check / Repair leave it
  // open so their results update the panels in place.
  hideEditPanel();
}

function onButtonClick(name) {
  var result = readEditedComponent();
  if (result.error) { setStatus(result.error, true); return; }
  var cb = currentCallbacks[name];
  if (typeof cb !== 'function') {
    setStatus('No ' + name + ' handler is wired up.', true);
    return;
  }
  setStatus('');
  cb(result.component);
}

// Keep the unit hint, colour availability, pickers and LED Vf hint
// in step with the panel inputs. Registered once, when the panel is
// first built (not at import time, so importing this module never
// touches the DOM). Document-level delegation: the panel DOM is
// created after this wiring runs.
var typeChangeWired = false;
function wireTypeChange() {
  if (typeChangeWired) return;
  typeChangeWired = true;
  document.addEventListener('change', function (e) {
    if (!e.target) return;
    var id = e.target.id;
    if (id === PANEL_ID + '-type') {
      var type = e.target.value;
      var unit = el(PANEL_ID + '-unit');
      if (unit) unit.textContent = valueUnit(type);
      var color = el(PANEL_ID + '-color');
      if (color) color.disabled = !(type === 'led' || type === 'diode');
      // Repopulate the picker for the new type. A custom value the
      // user already typed stays Custom… (valueCustomMode); otherwise
      // the nearest standard is preselected, or Custom… when the old
      // value is orders of magnitude from the new type's list.
      populateValuePicker();
      refreshLedHint(color ? color.value : '', true);
    } else if (id === PANEL_ID + '-value-pick') {
      var pick = e.target;
      var input = el(PANEL_ID + '-value');
      if (!input) return;
      if (pick.value === 'custom') {
        // Custom… keeps whatever value is in the number input and
        // shows it, focused, for free-text entry.
        valueCustomMode = true;
        input.style.display = '';
        input.focus();
        var hint = el(PANEL_ID + '-value-hint');
        if (hint) hint.textContent = '';
      } else {
        // Choosing a standard entry writes its SI float into the
        // number input, so readEditedComponent() needs no change.
        valueCustomMode = false;
        input.value = pick.value;
        input.style.display = 'none';
        var hint2 = el(PANEL_ID + '-value-hint');
        if (hint2) hint2.textContent = '';
      }
    } else if (id === PANEL_ID + '-rating-pick') {
      var rpick = e.target;
      var rinput = el(PANEL_ID + '-rating');
      if (!rinput) return;
      if (rpick.value === 'custom') {
        ratingCustomMode = true;
        rinput.style.display = '';
        rinput.focus();
      } else {
        ratingCustomMode = false;
        rinput.value = rpick.value;
        rinput.style.display = 'none';
      }
    } else if (id === PANEL_ID + '-color') {
      // Colour change never overwrites the value: show the typical
      // Vf with a "Use Vf" button instead.
      refreshLedHint(e.target.value, false);
    }
  });
  // Typing in a visible number input means the value is custom, so a
  // catalog load landing afterwards must not overwrite it with a
  // picker selection.
  document.addEventListener('input', function (e) {
    if (!e.target) return;
    if (e.target.id === PANEL_ID + '-value') valueCustomMode = true;
    else if (e.target.id === PANEL_ID + '-rating') ratingCustomMode = true;
  });
}

/* ------------------------------------------------------------------ */
/* Public API                                                          */
/* ------------------------------------------------------------------ */

/**
 * Show the edit panel for one component.
 *
 * @param {object} component - Component object { id, type, value,
 *   nodes: [n1, n2], rating_watts?, color? }.
 * @param {object|null} report - Latest /check Report, or null when no
 *   check has run yet. Measurements and findings come from this only.
 * @param {object} callbacks - { onSave, onRecheck?, onRepair?, onClose? }.
 *   onSave(editedComponent) is required; the page then POSTs /check.
 */
export function showEditPanel(component, report, callbacks) {
  if (!component || typeof component !== 'object' || !component.id) {
    throw new Error('showEditPanel: a component object with an id is required');
  }
  if (!callbacks || typeof callbacks.onSave !== 'function') {
    throw new Error('showEditPanel: callbacks.onSave is required');
  }
  // Capture the invoker BEFORE rendering: whichever control had focus
  // when the panel opened is where focus returns on close. A
  // double-click leaves focus on <body>, which is not a useful
  // invoker — the callout / #scene3d fallbacks cover that.
  var active = (typeof document !== 'undefined') ? document.activeElement : null;
  invoker = (active && active !== document.body &&
    active !== document.documentElement) ? active : null;
  lastComponentId = String(component.id);
  // Copy so later panel edits never mutate the caller's object.
  currentComponent = {
    id: component.id,
    type: component.type,
    value: component.value,
    nodes: Array.isArray(component.nodes) ? component.nodes.slice() : [],
    rating_watts: component.rating_watts != null ? component.rating_watts : null,
    color: component.color != null ? component.color : null,
    node_voltage: component.node_voltage,
    voltage: component.voltage,
    current: component.current,
    power: component.power,
  };
  currentReport = report || null;
  currentCallbacks = callbacks;
  renderPanel();
}

/** Hide the edit panel, clear the selected component, and return focus. */
export function hideEditPanel() {
  var panel = el(PANEL_ID);
  clearLeader();
  if (panel) panel.style.display = 'none';
  var onClose = currentCallbacks && currentCallbacks.onClose;
  var hadComponent = currentComponent != null;
  currentComponent = null;
  currentReport = null;
  currentCallbacks = {};
  // Focus returns even if onClose throws; the error still propagates.
  try {
    if (typeof onClose === 'function') onClose();
  } finally {
    if (hadComponent) focusInvoker();
    invoker = null;
    lastComponentId = null;
    selectEscArmed = false;
  }
}
