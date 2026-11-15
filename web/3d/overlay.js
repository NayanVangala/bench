/**
 * Netproof 3D overlay — web/3d/overlay.js
 *
 * Floating live-data labels + current-flow animation for the Netproof 3D
 * circuit view. Three.js for the dots, plain DOM elements for the labels.
 *
 * This module NEVER invents numbers. Every value it shows comes from the
 * Report object handed to updateOverlays(), which the page fills from
 * ngspice via the server. A value that is absent, null, or non-finite in
 * the Report is simply omitted from the label, never guessed or defaulted.
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
 *   updateOverlays(report, meshesById) — refresh labels + component colours.
 *   animateFlow(deltaTime, branchCurrents) — advance the flow dots one frame.
 *
 * Wiring (the 3D scene module owns the renderer / camera / meshes):
 *   import { initOverlays, updateOverlays, animateFlow, updateLabelPositions }
 *     from './3d/overlay.js';
 *   initOverlays({ THREE, scene, camera, container, wiresById, nodesById });
 *   // after every /check or /run result:
 *   updateOverlays(report, meshesById);
 *   // inside the render loop:
 *   animateFlow(deltaTimeSeconds, report.branch_currents);
 *   updateLabelPositions();   // re-project labels after the camera moves
 *
 * Notes for the integrator:
 * - THREE is NOT imported here. Pass it to initOverlays() or leave it on
 *   window.THREE; without it, labels still get text + colours, but
 *   projection and flow dots are skipped.
 * - meshesById: { [componentId]: THREE.Object3D } — a Mesh or a Group.
 * - wiresById:  { [componentId]: [Vector3, ...] | THREE.Curve | { points } }
 *   A wire's point order defines "positive" flow direction: a positive
 *   branch current moves dots from the first point to the last, a negative
 *   current reverses them. Keep the order matching nodes [n1, n2].
 * - This file is standalone: it does not touch web/index.html or the
 *   backend, and it makes no network calls.
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const SEVERITY_COLORS = {
  STOP: '#d92d20', // red
  WARN: '#d97706', // amber
  PASS: '#1a7f37', // green
  INFO: '#667085', // grey (never used for tinting; INFO never colours a part)
  UNKNOWN: '#8b949e', // grey, matches scene.js VERDICT_COLORS.UNKNOWN
};

const SEVERITY_RANK = { STOP: 0, WARN: 1, INFO: 2 };

const DOTS_PER_BRANCH = 3;
const DOT_COLOR = 0xffb020;
const DOT_RADIUS_DEFAULT = 0.06;
// Fraction of wire length travelled per second. Scales with log current so
// a 15 mA LED current is clearly visible without a 2 A source teleporting.
const FLOW_BASE_RATE = 0.05;
const FLOW_RATE_PER_DECADE = 0.12;
const FLOW_REFERENCE_CURRENT_A = 0.001; // 1 mA
const MAX_DELTA_TIME_S = 0.25;

// ---------------------------------------------------------------------------
// Module state (set by initOverlays; everything else degrades gracefully)
// ---------------------------------------------------------------------------

let THREE = null;
let scene = null;
let camera = null;
let labelLayer = null;
let styleInjected = false;

let wiresById = {}; // { [branchId]: wire definition }
let nodesById = {}; // { [componentId]: [n1, n2] } — optional integrator override

const labels = new Map(); // componentId -> { el, bodyEl, anchor: Vector3|null }
const flows = new Map(); // branchId -> { points, cumLen, total, dots, phase }
const tintedMaterials = new Set(); // materials we recoloured, for restore

let dotGeometry = null;
let dotMaterial = null;
let dotRadius = DOT_RADIUS_DEFAULT;

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
 * labels need a camera to be positioned and dots need a scene + THREE.
 *
 * options: {
 *   THREE,            // the Three.js namespace (else window.THREE)
 *   scene,            // THREE.Scene — flow dots are added here
 *   camera,           // THREE.Camera — labels are projected with this
 *   container,        // element the label layer is appended to (default: document.body)
 *   wiresById,        // { [branchId]: [Vector3...] | Curve | { points } }
 *   nodesById,        // { [componentId]: [n1, n2] } (else taken from report.netlist_used)
 *   dotRadius,        // world-units radius of a flow dot
 * }
 */
export function initOverlays(options) {
  const opts = options || {};
  if (opts.THREE) THREE = opts.THREE;
  else if (typeof window !== 'undefined' && window.THREE) THREE = window.THREE;
  if (opts.scene) scene = opts.scene;
  if (opts.camera) camera = opts.camera;
  if (opts.wiresById) wiresById = opts.wiresById;
  if (opts.nodesById) nodesById = opts.nodesById;
  if (isNum(opts.dotRadius) && opts.dotRadius > 0) {
    dotRadius = opts.dotRadius;
    dotGeometry = null; // rebuild at the new size on next use
  }
  ensureLabelLayer(opts.container);
  return { labelLayer };
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

function ensureLabelLayer(container) {
  if (typeof document === 'undefined') return null;
  if (labelLayer) return labelLayer;
  injectLabelStyles();
  const layer = document.createElement('div');
  layer.className = 'netproof-label-layer';
  const host = container || document.body;
  layer.style.position = host === document.body ? 'fixed' : 'absolute';
  layer.style.left = '0';
  layer.style.top = '0';
  layer.style.right = '0';
  layer.style.bottom = '0';
  layer.style.overflow = 'hidden';
  layer.style.pointerEvents = 'none';
  layer.style.zIndex = '5';
  host.appendChild(layer);
  labelLayer = layer;
  return layer;
}

function injectLabelStyles() {
  if (styleInjected || typeof document === 'undefined') return;
  const style = document.createElement('style');
  style.textContent =
    '.netproof-label{position:absolute;left:0;top:0;display:none;min-width:92px;' +
    'padding:4px 7px;border:2px solid #1a7f37;border-radius:6px;' +
    'background:rgba(255,255,255,0.92);color:#111;' +
    'font:12px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;' +
    'pointer-events:none;white-space:pre-line;box-shadow:0 1px 4px rgba(0,0,0,0.25);}' +
    '.netproof-label-title{font-weight:700;font-size:12px;}' +
    '.netproof-label-body{font-size:11px;color:#222;}' +
    '.netproof-label-nodata{color:#777;font-style:italic;}';
  document.head.appendChild(style);
  styleInjected = true;
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
// Labels
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

function ensureLabel(id) {
  let label = labels.get(id);
  if (label) return label;
  ensureLabelLayer();
  const el = document.createElement('div');
  el.className = 'netproof-label';
  el.dataset.componentId = id;
  const titleEl = document.createElement('div');
  titleEl.className = 'netproof-label-title';
  titleEl.textContent = id;
  const bodyEl = document.createElement('div');
  bodyEl.className = 'netproof-label-body';
  el.appendChild(titleEl);
  el.appendChild(bodyEl);
  if (labelLayer) labelLayer.appendChild(el);
  label = { el, bodyEl, anchor: null };
  labels.set(id, label);
  return label;
}

/**
 * Label lines for one component. Only values present (and finite) in the
 * Report are shown; a missing value leaves its line out entirely.
 * Line 1: node voltages, e.g. "vcc 9.00 V · led 2.00 V"
 * Line 2: current and power, e.g. "14.9 mA · 104.3 mW"
 */
function buildLabelLines(id, nodes, nodeVoltages, branchCurrents, power) {
  const lines = [];
  const voltParts = [];
  for (const node of nodes || []) {
    const v = lookupCI(nodeVoltages, node);
    if (isNum(v)) voltParts.push(node + ' ' + fmtVolts(v));
  }
  if (voltParts.length) lines.push(voltParts.join(' · '));
  const ipParts = [];
  const i = lookupCI(branchCurrents, id);
  if (isNum(i)) ipParts.push(fmtAmps(i));
  const p = lookupCI(power, id);
  if (isNum(p)) ipParts.push(fmtWatts(p));
  if (ipParts.length) lines.push(ipParts.join(' · '));
  return lines;
}

/** World-space anchor for a label: top-centre of the mesh's bounding box. */
function computeAnchor(mesh) {
  if (!THREE || !mesh) return null;
  try {
    if (typeof THREE.Box3 === 'function') {
      const box = new THREE.Box3().setFromObject(mesh);
      const center = box.getCenter(new THREE.Vector3());
      return new THREE.Vector3(center.x, box.max.y + 0.15, center.z);
    }
    if (typeof mesh.getWorldPosition === 'function') {
      return mesh.getWorldPosition(new THREE.Vector3());
    }
  } catch (e) {
    // A mesh with no geometry (or not yet in the scene) has no anchor;
    // its label stays hidden rather than piling up at the origin.
    return null;
  }
  return null;
}

/**
 * Refresh all labels and component colours from a Report.
 *
 *   updateOverlays(report, meshesById)
 *
 * meshesById: { [componentId]: THREE.Object3D }. Labels are created for
 * every mesh present and removed for components that disappeared. An
 * optional third argument { nodesById } overrides node lookup for a call.
 */
export function updateOverlays(report, meshesById, extra) {
  if (!report || typeof report !== 'object') return;
  const meshes = meshesById || {};
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

  const seen = new Set();
  for (const id of Object.keys(meshes)) {
    const mesh = meshes[id];
    if (!mesh) continue;
    seen.add(id);

    // Colour: worst finding touching this component. An untouched
    // component is PASS green only when the backend verdict is PASS
    // overall and no severe global finding (e.g. NO_GROUND, which
    // names no component) blocks that claim; otherwise it is UNKNOWN
    // grey, like scene.js — the 3D view never infers a PASS itself.
    // A null from severityForComponent (no verdict supplied) also
    // resolves to UNKNOWN, never to green.
    const sev = severityForComponent(id, findings, report.verdict) || 'UNKNOWN';
    const color = SEVERITY_COLORS[sev];
    tintMesh(mesh, color);

    // Label text — numbers straight from the Report, nothing invented.
    const label = ensureLabel(id);
    label.el.style.borderColor = color;
    label.el.dataset.severity = sev;
    const lines = buildLabelLines(id, nodes[id], nodeVoltages, branchCurrents, power);
    if (lines.length) {
      label.bodyEl.textContent = lines.join('\n');
      label.bodyEl.classList.remove('netproof-label-nodata');
    } else {
      label.bodyEl.textContent = 'no sim data';
      label.bodyEl.classList.add('netproof-label-nodata');
    }
    label.anchor = computeAnchor(mesh);
  }

  // Drop labels for components no longer in the scene.
  for (const [id, label] of labels) {
    if (!seen.has(id)) {
      if (label.el && typeof label.el.remove === 'function') label.el.remove();
      labels.delete(id);
    }
  }

  updateLabelPositions();
}

// ---------------------------------------------------------------------------
// Label projection (call every frame after the camera moves)
// ---------------------------------------------------------------------------

/**
 * Re-project every label from its 3D anchor to screen pixels.
 * Pass the camera, or rely on the one given to initOverlays().
 * Labels whose anchor is behind the camera are hidden.
 *
 * Overlap: the LED circuit's parts sit 1.5 scene units apart on one
 * line (V1, R1, D1), so their full labels (~170 px wide at the framed
 * distance) land on the same screen row and overlap. Labels are
 * therefore placed left-to-right and any label whose box would overlap
 * an already-placed one is stacked one label-height up (and, if there
 * is no room above the container, below its anchor instead) until it
 * is clear. Full label text is kept for every part; nothing is
 * shortened to a tag. Recomputed from scratch every frame, so a
 * camera move re-stacks the labels for the new projection.
 */
export function updateLabelPositions(cam) {
  const activeCam = cam || camera;
  if (!THREE || !activeCam || !labelLayer) return;
  const width = labelLayer.clientWidth || (typeof window !== 'undefined' ? window.innerWidth : 0);
  const height = labelLayer.clientHeight || (typeof window !== 'undefined' ? window.innerHeight : 0);
  if (!width || !height) return;
  const GAP = 6; // px kept clear between stacked labels
  const LIFT = 8; // px between a label's bottom edge and its anchor
  const v = new THREE.Vector3();
  const projected = [];
  for (const label of labels.values()) {
    if (!label.anchor) {
      label.el.style.display = 'none';
      continue;
    }
    v.copy(label.anchor).project(activeCam);
    if (v.z > 1 || v.z < -1) {
      label.el.style.display = 'none';
      continue;
    }
    const x = (v.x * 0.5 + 0.5) * width;
    const y = (-v.y * 0.5 + 0.5) * height;
    label.el.style.display = 'block';
    projected.push({ label, x, y });
  }
  // Leftmost keeps its lane; neighbours stack up from it.
  projected.sort((a, b) => a.x - b.x || a.y - b.y);
  const placed = []; // { left, right, top, bottom } in container px
  for (const { label, x, y } of projected) {
    const w = label.el.offsetWidth || 92;
    const h = label.el.offsetHeight || 48;
    let dy = 0;
    let below = false; // false: stack up; true: no room above, stack down
    for (let attempt = 0; attempt < 12; attempt++) {
      const box = {
        left: x - w / 2,
        right: x + w / 2,
        top: y - h - LIFT + dy,
        bottom: y - LIFT + dy,
      };
      const hit = placed.find((p) =>
        box.left < p.right + GAP && box.right > p.left - GAP &&
        box.top < p.bottom + GAP && box.bottom > p.top - GAP);
      if (!hit) break;
      if (!below && box.top - (h + GAP) < 0) {
        // No room for another lane above: drop below the anchor.
        below = true;
        dy = h + 2 * LIFT;
      } else if (below) {
        dy += h + GAP;
      } else {
        dy -= h + GAP;
      }
    }
    placed.push({
      left: x - w / 2,
      right: x + w / 2,
      top: y - h - LIFT + dy,
      bottom: y - LIFT + dy,
    });
    label.el.style.transform =
      'translate(-50%, calc(-100% - 8px)) translate(' + x.toFixed(1) + 'px, ' + (y + dy).toFixed(1) + 'px)';
  }
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

/**
 * Advance the current-flow dots one frame.
 *
 *   animateFlow(deltaTimeSeconds, branchCurrents)
 *
 * branchCurrents: { [componentId]: amps } from the Report. Dots appear
 * only while the current is non-zero; speed grows with log|I| and the
 * sign picks the direction (positive = along the stored wire points,
 * negative = reversed). Call every frame from the render loop.
 */
export function animateFlow(deltaTime, branchCurrents) {
  if (!THREE || !scene) return;
  const dt = Math.min(Math.max(Number(deltaTime) || 0, 0), MAX_DELTA_TIME_S);
  const currents = branchCurrents || {};

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
    const rate = FLOW_BASE_RATE +
      FLOW_RATE_PER_DECADE * Math.log10(1 + Math.abs(i) / FLOW_REFERENCE_CURRENT_A);
    flow.phase = (((flow.phase + direction * rate * dt) % 1) + 1) % 1;
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
  for (const label of labels.values()) {
    if (label.el && typeof label.el.remove === 'function') label.el.remove();
  }
  labels.clear();
  for (const flow of flows.values()) {
    for (const dot of flow.dots) {
      if (scene) scene.remove(dot);
    }
  }
  flows.clear();
  if (labelLayer && typeof labelLayer.remove === 'function') labelLayer.remove();
  labelLayer = null;
  restoreMaterials();
  dotGeometry = null;
  dotMaterial = null;
  scene = null;
  camera = null;
}
