/**
 * Netproof 3D edit panel — web/3d/edit-panel.js
 *
 * Plain JS module (no build step, no CDN, no LLM calls, no fetch).
 * Shows an edit panel for the component selected in the 3D view.
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
 *   onRecheck(editedComponent) — optional. Re-check button.
 *   onRepair(editedComponent)  — optional. Repair button.
 *   onClose()                  — optional. Called when the panel closes.
 * Buttons whose callback is missing are disabled.
 */

'use strict';

var PANEL_ID = 'netproof-edit-panel';
var STYLE_ID = 'netproof-edit-panel-style';

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

var currentComponent = null; // original component object passed to showEditPanel
var currentReport = null;
var currentCallbacks = {};

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

/* ------------------------------------------------------------------ */
/* Panel DOM                                                           */
/* ------------------------------------------------------------------ */

function injectStyle() {
  if (el(STYLE_ID)) return;
  var style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = [
    '#' + PANEL_ID + ' {',
    '  position: fixed; top: 16px; right: 16px; z-index: 1000;',
    '  width: 320px; max-height: calc(100vh - 32px); overflow-y: auto;',
    '  background: #fff; color: #111; border: 1px solid #bbb; border-radius: 8px;',
    '  box-shadow: 0 4px 24px rgba(0,0,0,.25); padding: 14px;',
    '  font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; font-size: 13px;',
    '}',
    '#' + PANEL_ID + ' h2 { margin: 0 0 4px; font-size: 16px; }',
    '#' + PANEL_ID + ' h3 { margin: 12px 0 6px; font-size: 13px; }',
    '#' + PANEL_ID + ' .ep-sub { color: #555; margin: 0 0 8px; }',
    '#' + PANEL_ID + ' label { display: block; font-weight: 600; font-size: 12px; margin: 8px 0 3px; }',
    '#' + PANEL_ID + ' input, #' + PANEL_ID + ' select {',
    '  width: 100%; padding: 6px 8px; font: inherit; font-size: 13px;',
    '  border: 1px solid #bbb; border-radius: 6px; background: #fff; color: #111;',
    '}',
    '#' + PANEL_ID + ' input:disabled, #' + PANEL_ID + ' select:disabled { background: #f4f4f4; color: #777; }',
    '#' + PANEL_ID + ' .ep-nodes { display: flex; gap: 8px; }',
    '#' + PANEL_ID + ' .ep-nodes > div { flex: 1; min-width: 0; }',
    '#' + PANEL_ID + ' .ep-hint { color: #777; font-size: 12px; font-weight: 400; }',
    '#' + PANEL_ID + ' table.ep-measure { border-collapse: collapse; width: 100%; font-size: 13px; }',
    '#' + PANEL_ID + ' table.ep-measure td { border: 1px solid #ddd; padding: 5px 7px; }',
    '#' + PANEL_ID + ' table.ep-measure td:first-child { font-weight: 600; width: 45%; background: #f6f7f9; }',
    '#' + PANEL_ID + ' ul.ep-findings { margin: 0; padding-left: 18px; }',
    '#' + PANEL_ID + ' ul.ep-findings li { margin-bottom: 6px; }',
    '#' + PANEL_ID + ' .ep-sev { font-weight: 700; font-size: 12px; text-transform: uppercase; }',
    '#' + PANEL_ID + ' .ep-sev-stop { color: #b00020; }',
    '#' + PANEL_ID + ' .ep-sev-warn { color: #b45309; }',
    '#' + PANEL_ID + ' .ep-sev-info { color: #555; }',
    '#' + PANEL_ID + ' .ep-empty { color: #777; }',
    '#' + PANEL_ID + ' .ep-actions { margin-top: 14px; }',
    '#' + PANEL_ID + ' button {',
    '  font: inherit; font-size: 13px; padding: 8px 12px; border-radius: 6px;',
    '  border: 1px solid #111; background: #111; color: #fff; cursor: pointer; margin: 4px 6px 4px 0;',
    '}',
    '#' + PANEL_ID + ' button.secondary { background: #fff; color: #111; }',
    '#' + PANEL_ID + ' button:disabled { opacity: .45; cursor: not-allowed; }',
    '#' + PANEL_ID + ' .ep-status { min-height: 18px; margin-top: 8px; font-size: 12px; color: #555; }',
    '#' + PANEL_ID + ' .ep-status.ep-error { color: #b00020; font-weight: 600; }',
    '#' + PANEL_ID + ' .ep-close { float: right; background: #fff; color: #111; border: 1px solid #bbb; padding: 2px 8px; margin: 0; }',
  ].join('\n');
  document.head.appendChild(style);
}

function buildPanel() {
  injectStyle();
  wireTypeChange();
  var panel = el(PANEL_ID);
  if (panel) return panel;

  panel = document.createElement('section');
  panel.id = PANEL_ID;
  panel.setAttribute('aria-label', 'Edit selected component');
  panel.innerHTML = [
    '<button type="button" class="ep-close" data-action="close" aria-label="Close edit panel">✕</button>',
    '<h2>Edit component</h2>',
    '<p class="ep-sub" id="' + PANEL_ID + '-sub"></p>',
    '<div><label>ID</label><div id="' + PANEL_ID + '-id"></div></div>',
    '<div><label for="' + PANEL_ID + '-type">Type</label>',
    '<select id="' + PANEL_ID + '-type">',
    COMPONENT_TYPES.map(function (t) {
      return '<option value="' + t + '">' + t + '</option>';
    }).join(''),
    '</select></div>',
    '<div><label for="' + PANEL_ID + '-value">Value <span class="ep-hint" id="' + PANEL_ID + '-unit"></span></label>',
    '<input id="' + PANEL_ID + '-value" type="number" step="any" inputmode="decimal"></div>',
    '<div><label>Nodes</label><div class="ep-nodes">',
    '<div><input id="' + PANEL_ID + '-node0" type="text" aria-label="First node" placeholder="node 1"></div>',
    '<div><input id="' + PANEL_ID + '-node1" type="text" aria-label="Second node" placeholder="node 2"></div>',
    '</div></div>',
    '<div><label for="' + PANEL_ID + '-rating">Power rating (W) <span class="ep-hint">resistors default 0.25 W</span></label>',
    '<input id="' + PANEL_ID + '-rating" type="number" step="any" min="0" inputmode="decimal" placeholder="0.25"></div>',
    '<div><label for="' + PANEL_ID + '-color">Colour <span class="ep-hint">LEDs only</span></label>',
    '<select id="' + PANEL_ID + '-color">',
    '<option value="">—</option>',
    LED_COLORS.map(function (c) {
      return '<option value="' + c + '">' + c + '</option>';
    }).join(''),
    '</select></div>',
    '<h3>Latest simulation</h3>',
    '<div id="' + PANEL_ID + '-measure"></div>',
    '<h3>Findings for this component</h3>',
    '<div id="' + PANEL_ID + '-findings"></div>',
    '<div class="ep-actions">',
    '<button type="button" data-action="save">Save</button>',
    '<button type="button" class="secondary" data-action="recheck">Re-check</button>',
    '<button type="button" class="secondary" data-action="repair">Repair</button>',
    '</div>',
    '<p class="ep-status" id="' + PANEL_ID + '-status" role="status"></p>',
  ].join('');

  panel.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action]');
    if (!btn) return;
    var action = btn.getAttribute('data-action');
    if (action === 'close') hideEditPanel();
    else if (action === 'save') onSaveClick();
    else if (action === 'recheck') onButtonClick('onRecheck');
    else if (action === 'repair') onButtonClick('onRepair');
  });

  document.body.appendChild(panel);
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

function renderPanel() {
  var c = currentComponent;
  if (!c) return;
  var panel = buildPanel();
  panel.style.display = 'block';

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

  renderMeasurements(c, currentReport);
  renderFindings(c, currentReport);
  setStatus('');

  // Re-check / Repair need a handler from the page; disable otherwise.
  var recheckBtn = panel.querySelector('[data-action="recheck"]');
  var repairBtn = panel.querySelector('[data-action="repair"]');
  if (recheckBtn) recheckBtn.disabled = typeof currentCallbacks.onRecheck !== 'function';
  if (repairBtn) repairBtn.disabled = typeof currentCallbacks.onRepair !== 'function';
  var saveBtn = panel.querySelector('[data-action="save"]');
  if (saveBtn) saveBtn.disabled = typeof currentCallbacks.onSave !== 'function';
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

// Keep the unit hint and colour availability in step with the type.
// Registered once, when the panel is first built (not at import time,
// so importing this module never touches the DOM).
var typeChangeWired = false;
function wireTypeChange() {
  if (typeChangeWired) return;
  typeChangeWired = true;
  document.addEventListener('change', function (e) {
    if (!e.target || e.target.id !== PANEL_ID + '-type') return;
    var type = e.target.value;
    var unit = el(PANEL_ID + '-unit');
    if (unit) unit.textContent = valueUnit(type);
    var color = el(PANEL_ID + '-color');
    if (color) color.disabled = !(type === 'led' || type === 'diode');
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

/** Hide the edit panel and clear the selected component. */
export function hideEditPanel() {
  var panel = el(PANEL_ID);
  if (panel) panel.style.display = 'none';
  var onClose = currentCallbacks && currentCallbacks.onClose;
  currentComponent = null;
  currentReport = null;
  currentCallbacks = {};
  if (typeof onClose === 'function') onClose();
}
