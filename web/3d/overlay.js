/**
 * Netproof 3D overlay — web/3d/overlay.js
 *
 * Blueprint callout system + current-flow animation for the Netproof
 * 3D circuit view. Three.js for the dots, plain DOM elements for the
 * callouts. The callout DOM/SVG/layout lives in ./callouts.js; this
 * module owns the Report feed, the mesh tinting, the flow dots, and
 * the pointer/keyboard wiring that opens a detail callout when a
 * part is clicked.
 *
 * WHAT WAS REMOVED (wholesale rebuild, 2026-11-15): the old floating
 * `.netproof-label` system is gone entirely — no label divs, no
 * label stacking, no per-part live-number labels, no
 * `.netproof-label-nodata`. Findings are no longer a sidebar list
 * rendered elsewhere; each finding is a callout box on the canvas
 * (see callouts.js). Nothing in this file renders a findings list.
 *
 * This module NEVER invents numbers. Every value it shows comes from
 * the Report object handed to updateOverlays(), which the page fills
 * from ngspice via the server. A value that is absent, null, or
 * non-finite in the Report is simply omitted (or shown as "—" by the
 * callout that owns the line), never guessed or defaulted.
 *
 * Report shape (backend/netproof/ir.py — Report):
 *   {
 *     verdict: "PASS" | "WARN" | "STOP",
 *     findings: [{ severity: "STOP"|"WARN"|"INFO", rule_id, component_ids: [id...], message }],
 *     node_voltages:   { [nodeName]: volts },      // e.g. { vcc: 9, led: 2.0, "0": 0 }
 *     branch_currents: { [componentId]: amps },   // keyed by component id
 *     power:           { [componentId]: watts },   // per-component power
 *     netlist_used:    { components: [{ id, type, value, nodes: [n1, n2] }] }
 *   }
 * Aliases tolerated: nodeVoltages, branchCurrents, per_component_power /
 * component_power, and a plain `netlist` array in place of netlist_used.
 *
 * Required exports:
 *   updateOverlays(report, meshesById) — refresh callouts + component colours.
 *   animateFlow(deltaTime, branchCurrents) — advance the flow dots one frame.
 *   updateLabelPositions(camera?) — re-project callouts (kept name:
 *     main3d.js calls it every frame; it now drives the callout
 *     edge-lane layout in callouts.js).
 *
 * Wiring (the 3D scene module owns the renderer / camera / meshes):
 *   import { initOverlays, updateOverlays, animateFlow, updateLabelPositions }
 *     from './3d/overlay.js';
 *   initOverlays({ THREE, scene, camera, container, wiresById, nodesById });
 *   // after every /check or /run result:
 *   updateOverlays(report, meshesById);
 *   // inside the render loop:
 *   animateFlow(deltaTimeSeconds, report.branch_currents);
 *   updateLabelPositions();   // re-project callouts after the camera moves
 *
 * Click a part (single click, under the 6 px threshold shared with
 * interaction.js) opens its detail callout; clicking empty space
 * closes the detail and any Repair diff callout. Escape closes the
 * detail callout first (interaction.js then deselects on the same
 * press). Callout activations dispatch a bubbling
 * "netproof:callout-select" CustomEvent on the container and call
 * options.onCalloutSelect when supplied; the page / main3d.js owns
 * table selection and the edit panel and listens for the event.
 *
 * Notes for the integrator:
 * - THREE is NOT imported here. Pass it to initOverlays() or leave it on
 *   window.THREE; without it, callouts still render (stacked, no
 *   leaders) and flow dots are skipped.
 * - meshesById: { [componentId]: THREE.Object3D } — a Mesh or a Group.
 * - wiresById:  { [branchId]: [Vector3, ...] | THREE.Curve | { points } }
 *   A wire's point order defines "positive" flow direction: a positive
 *   branch current moves dots from the first point to the last, a negative
 *   current reverses them. Keep the order matching nodes [n1, n2].
 * - This file is standalone: it does not touch web/index.html or the
 *   backend, and it makes no network calls.
 * - Callout API (initCallouts, updateCallouts, layoutCallouts,
 *   openDetailCallout, closeDetailCallout, showRepairDiffs,
 *   closeRepairDiffs, diffNetlists, parseMeasured, fmtValueForType)
 *   is implemented in ./callouts.js and re-exported here so existing
 *   import sites keep working.
 */

// ---------------------------------------------------------------------------
// Callout system (./callouts.js) — re-exported for existing import sites
// ---------------------------------------------------------------------------

import {
  initCallouts,
  updateCallouts,
  layoutCallouts,
  openDetailCallout,
  closeDetailCallout,
  getDetailComponentId,
  showRepairDiffs,
  closeRepairDiffs,
  setRepairRechecking,
  disposeCallouts,
  diffNetlists,
  parseMeasured,
  fmtValueForType,
} from './callouts.js';

export {
  initCallouts,
  updateCallouts,
  layoutCallouts,
  openDetailCallout,
  closeDetailCallout,
  getDetailComponentId,
  showRepairDiffs,
  closeRepairDiffs,
  setRepairRechecking,
  disposeCallouts,
  diffNetlists,
  parseMeasured,
  fmtValueForType,
};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// Severity -> mesh tint colour. Coordinator token fix (2026-11-15,
// applied to all three verdict-colour tables together — this file,
// scene.js VERDICT_COLORS, history.js VERDICT_COLORS):
// WARN was #d97706 (3.19:1 on white, fails AA as text) -> #92400E
// (7.09:1); UNKNOWN was #8b949e (3.08:1, fails) -> #4B5563 (7.56:1).
// STOP #d92d20 (4.83:1) and PASS #1a7f37 (5.08:1) already pass AA as
// text and are unchanged; INFO #667085 (4.97:1) passes and is
// unchanged. The mesh tint is dimmer for WARN now — that is the
// token set, and the blueprint direction replaces emissive tint with
// edge colour anyway (scene.js / callouts.js own that branch).
export const SEVERITY_COLORS = {
  STOP: '#d92d20', // red
  WARN: '#92400E', // amber, AA text token (was #d97706)
  PASS: '#1a7f37', // green
  INFO: '#667085', // grey (never used for tinting; INFO never colours a part)
  UNKNOWN: '#4B5563', // grey, AA text token (was #8b949e)
};

const DOTS_PER_BRANCH = 3;
const DOT_COLOR = 0xffb020;
const DOT_RADIUS_DEFAULT = 0.06;
// Fraction of wire length travelled per second. Scales with log current so
// a 15 mA LED current is clearly visible without a 2 A source teleporting.
const FLOW_BASE_RATE = 0.05;
const FLOW_RATE_PER_DECADE = 0.12;
const FLOW_REFERENCE_CURRENT_A = 0.001; // 1 mA
const MAX_DELTA_TIME_S = 0.25;
const CLICK_MOVE_THRESHOLD_PX = 6; // mirrors interaction.js

// ---------------------------------------------------------------------------
// Module state (set by initOverlays; everything else degrades gracefully)
// ---------------------------------------------------------------------------

let THREE = null;
let scene = null;
let camera = null;
let container = null;
let calloutHandle = null;

let wiresById = {}; // { [branchId]: wire definition }
let nodesById = {}; // { [componentId]: [n1, n2] } — optional integrator override

const flows = new Map(); // branchId -> { points, cumLen, total, dots, phase }
const tintedMaterials = new Set(); // materials we recoloured, for restore

let lastMeshes = {};
let lastReport = null;
let lastNodesById = {};

let dotGeometry = null;
let dotMaterial = null;
let dotRadius = DOT_RADIUS_DEFAULT;

let pickDown = null; // { x, y } of the last pointerdown, for click detection
let listenersAttached = false;

// ---------------------------------------------------------------------------
// Small helpers — numbers only ever come from the Report, never made up
// ---------------------------------------------------------------------------

function isNum(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function fmtVolts(v) {
  const a = Math.abs(v);
  if (a >= 10) return v.toFixed(1) + ' V';
  if (a >= 1) return v.toFixed(2) + ' V';
  return v.toFixed(3) + ' V';
}

function fmtAmps(i) {
  const a = Math.abs(i);
  if (a >= 1) return i.toFixed(2) + ' A';
  if (a >= 1e-3) return (i * 1e3).toFixed(1) + ' mA';
  if (a >= 1e-6) return (i * 1e6).toFixed(1) + ' uA';
  return (i * 1e9).toFixed(0) + ' nA';
}

function fmtWatts(p) {
  const a = Math.abs(p);
  if (a >= 1) return p.toFixed(2) + ' W';
  if (a >= 1e-3) return (p * 1e3).toFixed(1) + ' mW';
  return (p * 1e6).toFixed(1) + ' uW';
}

// Case-insensitive map lookup. Report keys are not one case: ngspice
// lowercases branch-current keys ("r1") while power keeps the
// component id ("R1"), and node names may be either. An exact lookup
// alone drops the current line (and, for uppercase nodes, the voltage
// line) — see P4: the edit panel showed "—" for current and the 3D
// labels showed power with no current until this landed.
function lookupCI(map, key) {
  if (!map || key == null) return undefined;
  if (map[key] != null) return map[key];
  const want = String(key).toLowerCase();
  for (const k of Object.keys(map)) {
    if (k.toLowerCase() === want) return map[k];
  }
  return undefined;
}

// Same, but returns the map's own key (for the flow-dot wiring, where
// the flows map is keyed by the wire id, not the Report's key).
function findKeyCI(map, key) {
  if (!map || key == null) return undefined;
  if (Object.prototype.hasOwnProperty.call(map, key)) return key;
  const want = String(key).toLowerCase();
  for (const k of Object.keys(map)) {
    if (k.toLowerCase() === want) return k;
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

/**
 * Wire the overlay into the scene. All options are optional except that
 * callouts need a camera to be positioned and dots need a scene + THREE.
 *
 * options: {
 *   THREE,            // the Three.js namespace (else window.THREE)
 *   scene,            // THREE.Scene — flow dots are added here
 *   camera,           // THREE.Camera — callouts are projected with this
 *   container,        // element the callout layer is appended to (default: document.body)
 *   wiresById,        // { [branchId]: [Vector3...] | Curve | { points } }
 *   nodesById,        // { [componentId]: [n1, n2] } (else taken from report.netlist_used)
 *   dotRadius,        // world-units radius of a flow dot
 *   onCalloutSelect,  // (componentId | null, source) — a callout was activated
 *   onSelect,         // alias of onCalloutSelect
 *   onCalloutActivate, // (componentId, kind) — Enter on a detail callout
 *   onActivate,       // alias of onCalloutActivate
 * }
 */
export function initOverlays(options) {
  const opts = options || {};
  if (opts.THREE) THREE = opts.THREE;
  else if (typeof window !== 'undefined' && window.THREE) THREE = window.THREE;
  if (opts.scene) scene = opts.scene;
  if (opts.camera) camera = opts.camera;
  if (opts.container) container = opts.container;
  if (opts.wiresById) wiresById = opts.wiresById;
  if (opts.nodesById) nodesById = opts.nodesById;
  if (isNum(opts.dotRadius) && opts.dotRadius > 0) {
    dotRadius = opts.dotRadius;
    dotGeometry = null; // rebuild at the new size on next use
  }
  const onSelect = typeof opts.onCalloutSelect === 'function' ? opts.onCalloutSelect
    : (typeof opts.onSelect === 'function' ? opts.onSelect : null);
  const onActivate = typeof opts.onCalloutActivate === 'function' ? opts.onCalloutActivate
    : (typeof opts.onActivate === 'function' ? opts.onActivate : null);
  calloutHandle = initCallouts({
    THREE,
    scene,
    camera,
    container,
    onSelect,
    onActivate,
  });
  attachPickListeners();
  return { labelLayer: calloutHandle ? calloutHandle.element : null, callouts: calloutHandle };
}

/** Register / replace wire paths after init. Drops cached dots for changed branches. */
export function setWires(nextWiresById) {
  if (!nextWiresById) return;
  for (const id of Object.keys(nextWiresById)) {
    wiresById[id] = nextWiresById[id];
    const flow = flows.get(id);
    if (flow) {
      for (const dot of flow.dots) {
        if (scene) scene.remove(dot);
      }
      flows.delete(id);
    }
  }
}

// ---------------------------------------------------------------------------
// Click a part -> detail callout (pointer pick, 6 px threshold)
// ---------------------------------------------------------------------------

function isInteractiveTarget(event) {
  const t = event.target;
  if (!t || typeof t.closest !== 'function') return false;
  return !!t.closest('.netproof-callout, button, input, select, a, .np-history');
}

function onPointerDown(event) {
  if (isInteractiveTarget(event)) {
    pickDown = null;
    return;
  }
  pickDown = { x: event.clientX, y: event.clientY };
}

function onPointerUp(event) {
  if (!pickDown) return;
  const dx = event.clientX - pickDown.x;
  const dy = event.clientY - pickDown.y;
  pickDown = null;
  // A drag is not a click (same 6 px threshold as interaction.js).
  if (dx * dx + dy * dy > CLICK_MOVE_THRESHOLD_PX * CLICK_MOVE_THRESHOLD_PX) return;
  if (isInteractiveTarget(event)) return;
  const id = pickComponentId(event);
  if (id) {
    openDetailCallout(id);
  } else {
    // Clicking empty space closes the detail callout and any Repair
    // diff callout; interaction.js deselects on the same press.
    closeDetailCallout();
    closeRepairDiffs();
  }
}

function pickComponentId(event) {
  if (!THREE || !camera || !container) return null;
  const rect = container.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const nx = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  const ny = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  const raycaster = new THREE.Raycaster();
  raycaster.setFromCamera(new THREE.Vector2(nx, ny), camera);
  const roots = Object.values(lastMeshes).filter(Boolean);
  if (!roots.length) return null;
  const hits = raycaster.intersectObjects(roots, true);
  for (const hit of hits) {
    let obj = hit.object;
    while (obj) {
      const cid = obj.userData && obj.userData.componentId;
      if (cid && lastMeshes[cid]) return String(cid);
      obj = obj.parent;
    }
  }
  return null;
}

// Escape closes the detail callout first; the event is left to
// propagate so interaction.js deselects on the same press.
function onKeyDown(event) {
  if (event.key !== 'Escape') return;
  if (getDetailComponentId()) closeDetailCallout();
}

function attachPickListeners() {
  if (listenersAttached || typeof window === 'undefined' || !container) return;
  container.addEventListener('pointerdown', onPointerDown);
  container.addEventListener('pointerup', onPointerUp);
  window.addEventListener('keydown', onKeyDown);
  listenersAttached = true;
}

function detachPickListeners() {
  if (!listenersAttached) return;
  if (container) {
    container.removeEventListener('pointerdown', onPointerDown);
    container.removeEventListener('pointerup', onPointerUp);
  }
  if (typeof window !== 'undefined') window.removeEventListener('keydown', onKeyDown);
  listenersAttached = false;
  pickDown = null;
}

// ---------------------------------------------------------------------------
// Component severity + colouring
// ---------------------------------------------------------------------------

/**
 * True when a finding names no component (a global finding such as
 * NO_GROUND) and is severe enough to block a PASS claim for any part.
 * Such a finding colours no component, so no untouched component may
 * be shown green on its account.
 */
function hasGlobalSevereFinding(findings) {
  for (const f of findings || []) {
    if (!f || typeof f !== 'object') continue;
    const sev = String(f.severity || f.level || '').toUpperCase();
    if (sev !== 'STOP' && sev !== 'WARN') continue;
    let ids = f.component_ids ?? f.componentIds ?? null;
    let list;
    if (Array.isArray(ids)) list = ids;
    else if (typeof ids === 'string') list = [ids];
    else if (f.component) list = [f.component];
    else if (f.id) list = [f.id];
    else list = [];
    if (!list.length) return true;
  }
  return false;
}

/**
 * Worst finding severity touching a component, from finding.component_ids.
 * Returns 'STOP' | 'WARN' when a finding touches it.
 * Returns null when no finding touches it AND no overall verdict was
 * supplied: null means "unknown, not PASS" — the caller must resolve it
 * against the backend verdict, never assume green.
 * With an overall verdict supplied (third argument, the Report's
 * verdict), an untouched component resolves to 'PASS' only when the
 * overall verdict is PASS and no severe global finding (e.g. NO_GROUND,
 * which names no component) blocks that claim; otherwise 'UNKNOWN'.
 * Findings with no component_ids (e.g. NO_GROUND) colour no component.
 * INFO findings never change a component's colour.
 */
export function severityForComponent(componentId, findings, overallVerdict) {
  let worst = null;
  for (const f of findings || []) {
    if (!f || typeof f !== 'object') continue;
    const ids = f.component_ids || f.componentIds ||
      (f.component ? [f.component] : null) ||
      (f.id ? [f.id] : null) || [];
    if (!ids.includes(componentId)) continue;
    const sev = String(f.severity || f.level || '').toUpperCase();
    if (sev === 'STOP') return 'STOP';
    if (sev === 'WARN' && worst !== 'STOP') worst = 'WARN';
    // INFO findings never change a component's colour.
  }
  if (worst) return worst;
  if (overallVerdict === undefined || overallVerdict === null) return null;
  const verdict = String(overallVerdict).trim().toUpperCase();
  if (verdict === 'PASS' && !hasGlobalSevereFinding(findings)) return 'PASS';
  return 'UNKNOWN';
}

function storeOriginalMaterial(mat) {
  if (!mat || !mat.userData) return;
  if (mat.userData.__netproofOrig) return;
  const orig = {};
  if (mat.emissive && typeof mat.emissive.clone === 'function') {
    orig.emissive = mat.emissive.clone();
  } else if (mat.emissive) {
    orig.emissiveHex = mat.emissive.getHex ? mat.emissive.getHex() : null;
  }
  if (typeof mat.emissiveIntensity === 'number') {
    orig.emissiveIntensity = mat.emissiveIntensity;
  }
  if (mat.color && typeof mat.color.clone === 'function') {
    orig.color = mat.color.clone();
  }
  mat.userData.__netproofOrig = orig;
  tintedMaterials.add(mat);
}

/**
 * Tint a component mesh/group by severity colour. STOP red, WARN amber,
 * PASS green. Uses emissive where the material has one (keeps the part
 * recognisable); falls back to the base colour for unlit materials.
 * Original material values are stored once so re-tinting restores cleanly.
 */
export function tintMesh(root, hexColor) {
  if (!root) return;
  const apply = (obj) => {
    const mats = obj.material
      ? (Array.isArray(obj.material) ? obj.material : [obj.material])
      : [];
    for (const mat of mats) {
      storeOriginalMaterial(mat);
      if (mat.emissive && typeof mat.emissive.set === 'function') {
        mat.emissive.set(hexColor);
        if (typeof mat.emissiveIntensity === 'number') mat.emissiveIntensity = 0.45;
      } else if (mat.color && typeof mat.color.set === 'function') {
        mat.color.set(hexColor);
      }
    }
  };
  if (typeof root.traverse === 'function') root.traverse(apply);
  else apply(root);
}

function restoreMaterials() {
  for (const mat of tintedMaterials) {
    const orig = mat.userData && mat.userData.__netproofOrig;
    if (!orig) continue;
    if (orig.emissive && mat.emissive && typeof mat.emissive.copy === 'function') {
      mat.emissive.copy(orig.emissive);
    } else if (orig.emissiveHex != null && mat.emissive && typeof mat.emissive.setHex === 'function') {
      mat.emissive.setHex(orig.emissiveHex);
    }
    if (typeof orig.emissiveIntensity === 'number') {
      mat.emissiveIntensity = orig.emissiveIntensity;
    }
    if (orig.color && mat.color && typeof mat.color.copy === 'function') {
      mat.color.copy(orig.color);
    }
    delete mat.userData.__netproofOrig;
  }
  tintedMaterials.clear();
}

// ---------------------------------------------------------------------------
// Report values per component (Report-only; handed to the callouts)
// ---------------------------------------------------------------------------

function nodesFromReport(report) {
  const out = {};
  const lists = [];
  if (report.netlist_used && Array.isArray(report.netlist_used.components)) {
    lists.push(report.netlist_used.components);
  }
  if (Array.isArray(report.netlist)) lists.push(report.netlist);
  if (Array.isArray(report.components)) lists.push(report.components);
  for (const list of lists) {
    for (const c of list) {
      if (c && c.id && Array.isArray(c.nodes)) out[c.id] = c.nodes;
    }
  }
  return out;
}

// Measured values for one component, straight from the Report.
// A missing value stays undefined here; the callout renders "—".
function componentMeasurements(id, nodes, nodeVoltages, branchCurrents, power) {
  const voltages = [];
  for (const node of nodes || []) {
    voltages.push({ node, value: lookupCI(nodeVoltages, node) });
  }
  return {
    voltages,
    current: lookupCI(branchCurrents, id),
    power: lookupCI(power, id),
  };
}

/**
 * Label lines for one component. Only values present (and finite) in the
 * Report are shown; a missing value leaves its line out entirely.
 * Line 1: node voltages, e.g. "vcc 9.00 V · led 2.00 V"
 * Line 2: current and power, e.g. "14.9 mA · 104.3 mW"
 * Kept for callers that still render a one-line summary; the canvas
 * callouts in callouts.js read componentMeasurements() instead.
 */
export function buildLabelLines(id, nodes, nodeVoltages, branchCurrents, power) {
  const m = componentMeasurements(id, nodes, nodeVoltages, branchCurrents, power);
  const lines = [];
  const voltParts = [];
  for (const v of m.voltages) {
    if (isNum(v.value)) voltParts.push(v.node + ' ' + fmtVolts(v.value));
  }
  if (voltParts.length) lines.push(voltParts.join(' · '));
  const ipParts = [];
  if (isNum(m.current)) ipParts.push(fmtAmps(m.current));
  if (isNum(m.power)) ipParts.push(fmtWatts(m.power));
  if (ipParts.length) lines.push(ipParts.join(' · '));
  return lines;
}

/**
 * Refresh callouts and component colours from a Report.
 *
 *   updateOverlays(report, meshesById)
 *
 * meshesById: { [componentId]: THREE.Object3D }. An optional third
 * argument { nodesById } overrides node lookup for a call.
 */
export function updateOverlays(report, meshesById, extra) {
  if (!report || typeof report !== 'object') return;
  const meshes = meshesById || {};
  lastMeshes = meshes;
  lastReport = report;
  const findings = Array.isArray(report.findings) ? report.findings : [];
  const nodeVoltages = report.node_voltages || report.nodeVoltages || {};
  const branchCurrents = report.branch_currents || report.branchCurrents || {};
  const power = report.power || report.per_component_power || report.component_power || {};
  const nodes = Object.assign(
    {},
    nodesById,
    nodesFromReport(report),
    (extra && extra.nodesById) || {}
  );
  lastNodesById = nodes;

  for (const id of Object.keys(meshes)) {
    const mesh = meshes[id];
    if (!mesh) continue;

    // Colour: worst finding touching this component. An untouched
    // component is PASS green only when the backend verdict is PASS
    // overall and no severe global finding (e.g. NO_GROUND, which
    // names no component) blocks that claim; otherwise it is UNKNOWN
    // grey, like scene.js — the 3D view never infers a PASS itself.
    // A null from severityForComponent (no verdict supplied) also
    // resolves to UNKNOWN, never to green.
    const sev = severityForComponent(id, findings, report.verdict) || 'UNKNOWN';
    // Only WARN / STOP glow; PASS and UNKNOWN keep the part's own colours
    // (black emissive = no tint on the lit materials).
    const color = sev === 'WARN' || sev === 'STOP' ? SEVERITY_COLORS[sev] : 0x000000;
    tintMesh(mesh, color);
  }

  // Per-component measured values for the detail callouts, straight
  // from the Report (current/power looked up case-insensitively:
  // ngspice lowercases branch-current keys while power keeps the id).
  const measuredById = {};
  for (const id of Object.keys(meshes)) {
    measuredById[id] = componentMeasurements(
      id, nodes[id], nodeVoltages, branchCurrents, power);
  }

  try {
    updateCallouts(report, meshes, { nodesById: nodes, measuredById });
  } catch (error) {
    // A callout failure must never break tinting or flow dots.
    if (typeof console !== 'undefined' && console.error) {
      console.error('[netproof 3D] callout update failed', error);
    }
  }
  updateLabelPositions();
}

// ---------------------------------------------------------------------------
// Callout projection (call every frame after the camera moves)
// ---------------------------------------------------------------------------

/**
 * Re-project every callout from its 3D anchor to screen pixels and
 * re-run the edge-lane layout from scratch, so orbit / zoom / pan
 * move the boxes and leader lines with the parts. Pass the camera,
 * or rely on the one given to initOverlays(). Kept name:
 * main3d.js calls updateLabelPositions(camera) in its render loop.
 */
export function updateLabelPositions(cam) {
  layoutCallouts(cam || camera);
}

// ---------------------------------------------------------------------------
// Current-flow dots
// ---------------------------------------------------------------------------

function normalizeWirePoints(wire) {
  if (!wire) return null;
  let raw = null;
  if (Array.isArray(wire)) raw = wire;
  else if (Array.isArray(wire.points)) raw = wire.points;
  else if (typeof wire.getPoints === 'function') raw = wire.getPoints(32);
  else if (wire.geometry && typeof wire.geometry.getPoints === 'function') {
    raw = wire.geometry.getPoints(32);
  }
  if (!raw || raw.length < 2) return null;
  const points = [];
  for (const p of raw) {
    if (!p) continue;
    if (Array.isArray(p) && isNum(p[0]) && isNum(p[1]) && isNum(p[2])) {
      points.push(new THREE.Vector3(p[0], p[1], p[2]));
    } else if (p.isVector3) {
      points.push(p.clone());
    } else if (isNum(p.x) && isNum(p.y) && isNum(p.z)) {
      points.push(new THREE.Vector3(p.x, p.y, p.z));
    }
  }
  return points.length >= 2 ? points : null;
}

function ensureFlow(branchId) {
  if (!THREE || !scene) return null;
  let flow = flows.get(branchId);
  if (flow) return flow;
  const points = normalizeWirePoints(wiresById[branchId]);
  if (!points) return null;
  const cumLen = [0];
  let total = 0;
  for (let s = 0; s < points.length - 1; s++) {
    total += points[s + 1].clone().sub(points[s]).length();
    cumLen.push(total);
  }
  if (!(total > 0)) return null;
  if (!dotGeometry) dotGeometry = new THREE.SphereGeometry(dotRadius, 12, 8);
  if (!dotMaterial) dotMaterial = new THREE.MeshBasicMaterial({ color: DOT_COLOR });
  const dots = [];
  for (let k = 0; k < DOTS_PER_BRANCH; k++) {
    const dot = new THREE.Mesh(dotGeometry, dotMaterial);
    dot.visible = false;
    scene.add(dot);
    dots.push(dot);
  }
  flow = { points, cumLen, total, dots, phase: 0 };
  flows.set(branchId, flow);
  return flow;
}

function pointAt(flow, u) {
  const target = ((u % 1) + 1) % 1 * flow.total;
  let seg = 0;
  while (seg < flow.cumLen.length - 2 && flow.cumLen[seg + 1] < target) seg++;
  const segLen = flow.cumLen[seg + 1] - flow.cumLen[seg];
  const t = segLen > 0 ? (target - flow.cumLen[seg]) / segLen : 0;
  return flow.points[seg].clone().lerp(flow.points[seg + 1], t);
}

function prefersReducedMotion() {
  return typeof window !== 'undefined' && window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * Advance the current-flow dots one frame.
 *
 *   animateFlow(deltaTimeSeconds, branchCurrents)
 *
 * branchCurrents: { [componentId]: amps } from the Report. Dots appear
 * only while the current is non-zero; speed grows with log|I| and the
 * sign picks the direction (positive = along the stored wire points,
 * negative = reversed). Call every frame from the render loop.
 * Under prefers-reduced-motion the dots do not travel: they render
 * once at their initial positions and stay static (current direction
 * is still shown as text in the callouts, never by motion alone).
 */
export function animateFlow(deltaTime, branchCurrents) {
  if (!THREE || !scene) return;
  const dt = Math.min(Math.max(Number(deltaTime) || 0, 0), MAX_DELTA_TIME_S);
  const currents = branchCurrents || {};
  const frozen = prefersReducedMotion();

  // Lazily build dot pools for wired branches that now carry current.
  // Branch-current keys are lowercase ("r1") while wire ids keep the
  // component id ("R1"): resolve the wire id case-insensitively, or
  // no flow is ever built (P4).
  for (const key of Object.keys(currents)) {
    const wireId = findKeyCI(wiresById, key) || key;
    if (!flows.has(wireId) && wiresById[wireId]) ensureFlow(wireId);
  }

  for (const [branchId, flow] of flows) {
    const i = lookupCI(currents, branchId);
    const active = isNum(i) && i !== 0;
    for (const dot of flow.dots) dot.visible = active;
    if (!active) continue;
    const direction = i > 0 ? 1 : -1;
    if (!frozen) {
      const rate = FLOW_BASE_RATE +
        FLOW_RATE_PER_DECADE * Math.log10(1 + Math.abs(i) / FLOW_REFERENCE_CURRENT_A);
      flow.phase = (((flow.phase + direction * rate * dt) % 1) + 1) % 1;
    }
    for (let k = 0; k < flow.dots.length; k++) {
      const u = flow.phase + k / flow.dots.length;
      flow.dots[k].position.copy(pointAt(flow, u));
    }
  }
}

// ---------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------

export function disposeOverlays() {
  detachPickListeners();
  try { disposeCallouts(); } catch (error) { /* callouts may never have initialised */ }
  calloutHandle = null;
  for (const flow of flows.values()) {
    for (const dot of flow.dots) {
      if (scene) scene.remove(dot);
    }
  }
  flows.clear();
  restoreMaterials();
  dotGeometry = null;
  dotMaterial = null;
  scene = null;
  camera = null;
  container = null;
  lastMeshes = {};
  lastReport = null;
  lastNodesById = {};
}
