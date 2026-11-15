// Netproof MVP — web/3d/main3d.js
//
// Plain ES module that wires the 3D layer together. It owns no geometry,
// no layout, no physics and no electrical numbers: every value it shows
// comes from its arguments (a netlist and/or a Report). It only
// orchestrates the sibling modules in this directory.
//
// Sibling-module contracts (verified against the files as they landed in
// web/3d/ — these are the real signatures, not guesses):
//
//   ./scene.js
//     createScene(container, { onPick }) -> view
//       { scene, camera, renderer, controls, components: Map,
//         raycaster, selectedId, pickAt, setOnPick, setSelected,
//         frameCamera, resetToIso, setExploded, toggleExplode,
//         isExploded, resetExplode, stepExplode, dispose }
//       buildDisplayed() calls view.resetToIso() directly (iso
//       direction + bounding-box fit) on load and on every
//       setNetlist / history-round rebuild, and view.resetExplode()
//       there too; it never calls frameCamera() (which would keep
//       the user's orbit) and a plain updateReport never rebuilds,
//       so it never resets the camera. handle.resetView delegates
//       to view.resetToIso() via resetViewInternal (which falls back
//       to an iso direction + frameCamera only when the scene
//       module predates resetToIso). The explode tween lives in
//       scene.js: handle.setExploded / toggleExplode delegate to
//       view.setExploded / view.toggleExplode, with a local tween
//       fallback only when the scene module predates them.
//       Runs its OWN animation loop (controls.update + render) and its
//       OWN resize handling (ResizeObserver). main3d.js therefore does
//       NOT render and does NOT resize; its loop only drives the
//       overlay (flow dots + label projection, see below) plus the
//       Inspect explode tween and the callout recompute.
//       main3d.js does not pass onPick: pointer selection callbacks
//       come from interaction.js. scene.js still does its scale
//       highlight on click; main3d.js keeps that in sync via
//       view.setSelected().
//     Component meshes built by components.js are registered in
//     view.components (id -> group) so scene.js raycast picking and
//     verdict-colour helpers can find them.
//     Orbit / pan / zoom live in scene.js (OrbitControls): left-drag
//     rotate, right-drag pan, wheel zoom. main3d.js only applies the
//     blueprint ortho limits (min/maxZoom, polar clamp) when the
//     camera actually is orthographic; a perspective camera keeps
//     scene.js's own distance limits untouched.
//
//   ./components.js
//     buildCircuit(netlist) -> { group, nodePositions, meshesById: Map }
//       Layout + mesh building live here (node grid by BFS hop distance
//       from ground, wires inside each component group). Throws on rows
//       without an id or without exactly two distinct nodes, so
//       main3d.js pre-filters those rows out (see normalizeNetlist).
//     normalizeNetlist(netlist) also exists but is strict (throws);
//       main3d.js uses its own lenient normaliser instead.
//
//   ./interaction.js
//     setupInteraction(scene, camera, renderer, meshesById, callbacks)
//       -> { dispose, select, deselect, getSelectedId, setMeshes,
//            setControls, refreshWires }
//       callbacks: onSelect(componentId | null), onMove(componentId,
//       { x, y, z }), onEdit(componentId) [double-click], plus optional
//       onDragStart / onDragEnd. meshesById may be a Map.
//       NOTE: this file imports bare 'three', so the host page must
//       carry an import map mapping 'three' to the same three.module.js
//       URL below (see the Three.js note). Without that map this whole
//       module graph fails to load in the browser.
//
//   ./overlay.js
//     initOverlays({ THREE, scene, camera, container, wiresById,
//                    nodesById, dotRadius }) -> { labelLayer }
//     updateOverlays(report, meshesById, extra?)
//       meshesById is a PLAIN OBJECT { [id]: Object3D } (not a Map);
//       extra.nodesById overrides the node lookup. Tints meshes by the
//       worst finding per component and shows live-number labels from
//       the Report only.
//     animateFlow(deltaTimeSeconds, branchCurrents) — call every frame.
//     updateLabelPositions(camera?) — call every frame after the camera
//       moves. disposeOverlays() on teardown. setWires({ [id]: points })
//       registers wire paths for the flow dots.
//     THREE is NOT imported here; initOverlays receives it from us.
//
//   ./edit-panel.js
//     showEditPanel(component, report, { onSave, onRecheck?, onRepair?,
//                    onClose? }) / hideEditPanel()
//       onSave(editedComponent) is required; the panel never POSTs —
//       the caller merges the edit and POSTs /check. Buttons without a
//       callback are disabled by the panel, so main3d.js only passes
//       onRecheck / onRepair when the page supplied them.
//
//   ./callouts.js — OPTIONAL, loaded only if it exists. Guarded
//     dynamic import: the blueprint callout layer (edge-docked boxes
//     + SVG leader lines) is a separate module owned by another
//     agent. If it has not landed yet, 3D still works without it;
//     a missing callouts.js is never reported as a 3D error.
//     Expected shape (mirrors overlay.js): initCallouts({ THREE,
//     scene, camera, container }) -> handle with
//     updateCallouts(report, meshesById) / update(report, meshesById),
//     a per-frame recompute — layoutCallouts(camera), also exported
//     as updatePositions / recompute / layout — and disposeCallouts()
//     / handle.dispose(). main3d.js
//     adapts to whichever of those names the module actually
//     exports; anything absent is skipped, never thrown.
//
// Blueprint wiring owned by this file (main3d.js):
//   * Camera: buildDisplayed() calls view.resetToIso() directly on
//     load and on every setNetlist / history-round / edit rebuild,
//     and view.resetExplode() there too. A plain updateReport (no
//     netlist change) never rebuilds, so it never resets the camera.
//   * Inspect explode: handle.setExploded(bool) /
//     handle.toggleExplode() delegate to view.setExploded() /
//     view.toggleExplode() (scene.js owns the tween) + the
//     #scene3d-section toggle button. Groups tween vertically
//     (300 ms, easeInOutCubic, staggered 0.9 + 0.18 * index), wires/slab
//     stay put, reduced-motion is instant, toggling mid-tween reverses
//     from the current Y. setNetlist / buildDisplayed resets exploded
//     to false via view.resetExplode().
//   * Reset view: handle.resetView() + the #scene3d-section button
//     + the R key when #scene3d itself has focus. Snaps back to iso
//     and refits the bounding box.
//   * Errors: init3D throws real Errors (never a generic "not
//     present"); the page bootstrap shows the real error text in
//     #scene3d-status and the flat table keeps working. Runtime
//     errors inside build/overlay go to onError with the real error,
//     which the page also surfaces verbatim in #scene3d-status.
//     side effect that sets window.NetproofHistory, which main3d.js
//     then reads via globalThis.
//     NetproofHistory.renderHistory(history, { onShowRound }, mount?)
//       -> { element, group, select, getSelectedIndex, getEntries,
//            attachScene, detachScene, update, destroy }
//       history entries: { round, netlist, report, changes }.
//       onShowRound(roundIndex, netlist, report) fires only on a real
//       click / select(), never on mount. attachScene({ scene, camera,
//       renderer }) adds the 3D timeline rail to the scene; it needs
//       window.THREE, so main3d.js sets window.THREE = THREE first
//       (restored on dispose). Mount: an element (or selector) to append
//       the panel to; without one the caller appends handle.element.
//
// Three.js: every module in this directory imports the bare specifier
// 'three' (and 'three/addons/...' for OrbitControls), resolved by the
// host page's import map to unpkg three@0.160.0. That map is the single
// source of the version, and because every import resolves to the same
// URL there is exactly one THREE instance in the page. Do NOT import
// three from a direct CDN URL here: a second URL would load a second
// instance and break instanceof / material sharing with OrbitControls
// and interaction.js. The import map belongs in web/index.html, which
// this file must not edit.
//
// Click vs double-click (per README-3D.md): click selects, double-click
// edits. main3d.js opens the edit panel on interaction onEdit only,
// unless the page passes editOnSelect: true.
//
// Dragging a component moves its mesh (and the wire tubes inside its
// group). Node marker dots are owned by the circuit group, not the
// component group, so they stay where layout put them; interaction.js's
// wire/node update hooks are not registered by buildCircuit. Layout
// only: a move never changes nodes/value, so onMove never fires
// onNetlistChange.
//
// Viewing a repair round is display-only: onShowRound swaps the
// DISPLAYED netlist/report (the scene shows that round) but the page's
// working netlist/report are untouched, so viewing history can never
// silently rewrite what the next /check or /repair POSTs. Any
// setNetlist / updateReport, or clearHistoryView(), returns the scene
// to the working netlist.
//
// Page usage (index.html integration is NOT done here — this file must
// not edit index.html; the page owner adds a container, the import map,
// and a module script that calls init3D):
//
//   import { init3D } from './3d/main3d.js';
//   const view = init3D(document.getElementById('netproof-3d'), {
//     onSelect: (id, component) => { /* highlight the table row */ },
//     onMove: (id, pose) => { /* layout-only; topology unchanged */ },
//     onEdit: (id, updated, netlist) => { /* POST /check, re-render */ },
//     onNetlistChange: (netlist) => { /* keep the page table in sync */ },
//     onShowRound: (index, netlist, report) => { /* history clicked */ },
//     onError: (error) => { /* surface in the error banner */ },
//   }, { netlist, report, history });
//   view.setNetlist(nextNetlist);      // after /run, /repair, table edits
//   view.updateReport(nextReport);     // after /check, /run, /repair
//   view.renderHistory(nextHistory);   // after /repair
//   view.dispose();                    // tear down (stops the loop)
//
// Netlist shapes accepted (all from the caller, never invented here):
//   { components: [{ id, type, value, nodes: [n1, n2],
//                    rating_watts?, color? }] }          (backend IR)
//   [{ id, type, value, nodes: [n1, n2] | "n1 n2",
//      node_voltage?, current?, power? }]                (index.html rows)
//   { netlist: <either of the above> }
// Rows without an id, or without exactly two distinct nodes, are
// skipped: buildCircuit cannot place them.
// Report shapes accepted: the backend Report
//   { verdict, findings: [{ severity, rule_id, component_ids, message }],
//     node_voltages, branch_currents, power, netlist_used, uncertain }
// and the index.html findings shape
//   { severity|level|type, message|text|detail, component|id|node }.
// Repair-history shapes accepted:
//   [{ round, netlist, report,
//      changes: [string | { description|message|text|summary }] }]
//   or a single such entry, or { history: [...] }.

import * as THREE from 'three';
import { createScene, frameCamera } from './scene.js';
import { buildCircuit } from './components.js';
import { partModelsReady } from './models.js';
import { setupInteraction } from './interaction.js';
import {
  initOverlays,
  updateOverlays,
  animateFlow,
  updateLabelPositions,
  disposeOverlays,
  setWires,
} from './overlay.js';
import { showEditPanel, hideEditPanel } from './edit-panel.js';
import './history.js'; // side effect only: sets window.NetproofHistory (UMD)

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

// ---------------------------------------------------------------------------
// Blueprint wiring helpers (spec §2.1, §2.4, §8)
// ---------------------------------------------------------------------------

// Classic isometric direction: elevation 35.264 deg, azimuth 45 deg,
// so dir = (cos(el)*sin(az), sin(el), cos(el)*cos(az)) ≈ (0.577, 0.577, 0.577).
const ISO_DIRECTION = { x: 0.5773502691896258, y: 0.5773502691896258, z: 0.5773502691896258 };
const EXPLODE_DURATION_MS = 300;
const EXPLODE_BASE_OFFSET = 0.9;
const EXPLODE_STEP_OFFSET = 0.18;

function easeInOutCubic(t) {
  const clamped = Math.min(1, Math.max(0, t));
  return clamped < 0.5
    ? 4 * clamped * clamped * clamped
    : 1 - Math.pow(-2 * clamped + 2, 3) / 2;
}

function prefersReducedMotion() {
  try {
    return typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (error) {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Normalisation: argument shapes in, canonical shapes out.
// No values are invented: a field that is absent stays absent.
// ---------------------------------------------------------------------------

function normalizeNodes(raw) {
  if (Array.isArray(raw)) return raw.map((n) => String(n));
  if (typeof raw === 'string') {
    return raw.split(/[\s,]+/).map((n) => n.trim()).filter(Boolean);
  }
  return [];
}

function normalizeComponent(raw) {
  if (!isObject(raw)) return null;
  const id = raw.id ?? raw.name ?? raw.refdes ?? raw.reference ?? raw.designator;
  if (id === undefined || id === null || String(id).trim() === '') return null;
  const nodes = normalizeNodes(raw.nodes ?? raw.pins ?? raw.terminals ?? raw.connections);
  // buildCircuit requires exactly two distinct nodes per component.
  if (nodes.length !== 2 || !nodes[0] || !nodes[1] || nodes[0] === nodes[1]) return null;
  const type = raw.type ?? raw.kind ?? raw.part_type ?? raw.part ?? '';
  const row = {
    id: String(id),
    type: String(type),
    value: raw.value ?? '',
    nodes,
  };
  // Optional fields, passed through only when the caller supplied them.
  if (raw.rating_watts !== undefined) row.rating_watts = raw.rating_watts;
  if (raw.rating_w !== undefined) row.rating_w = raw.rating_w;
  if (raw.power_rating !== undefined) row.power_rating = raw.power_rating;
  if (raw.color !== undefined) row.color = raw.color;
  if (raw.node_voltage !== undefined) row.node_voltage = raw.node_voltage;
  if (raw.voltage !== undefined) row.voltage = raw.voltage;
  if (raw.current !== undefined) row.current = raw.current;
  if (raw.power !== undefined) row.power = raw.power;
  return row;
}

export function normalizeNetlist(input) {
  if (!input) return [];
  let rawList = null;
  if (Array.isArray(input)) rawList = input;
  else if (isObject(input)) {
    if (Array.isArray(input.components)) rawList = input.components;
    else if (Array.isArray(input.parts)) rawList = input.parts;
    else if (Array.isArray(input.elements)) rawList = input.elements;
    else if (Array.isArray(input.netlist)) rawList = input.netlist;
    else if (isObject(input.netlist) && Array.isArray(input.netlist.components)) {
      rawList = input.netlist.components;
    }
  }
  if (!Array.isArray(rawList)) return [];
  const out = [];
  for (const raw of rawList) {
    const row = normalizeComponent(raw);
    if (row) out.push(row);
  }
  return out;
}

function normalizeFinding(raw) {
  if (typeof raw === 'string') {
    return { severity: '', rule_id: '', component_ids: [], message: raw };
  }
  if (!isObject(raw)) return null;
  const componentIds = raw.component_ids ?? raw.componentIds ?? raw.components ?? [];
  const ids = Array.isArray(componentIds)
    ? componentIds.map((c) => String(c))
    : [];
  const single = raw.component ?? raw.id ?? raw.node;
  if (single !== undefined && single !== null && String(single) !== '' && !ids.includes(String(single))) {
    ids.push(String(single));
  }
  return {
    severity: String(raw.severity ?? raw.level ?? raw.type ?? '').toUpperCase(),
    rule_id: String(raw.rule_id ?? raw.code ?? raw.rule ?? ''),
    component_ids: ids,
    message: String(raw.message ?? raw.text ?? raw.detail ?? ''),
    component: raw.component !== undefined ? String(raw.component) : undefined,
    node: raw.node !== undefined ? String(raw.node) : undefined,
  };
}

function normalizeNumberMap(raw) {
  if (!isObject(raw)) return {};
  const out = {};
  for (const [key, value] of Object.entries(raw)) {
    if (isFiniteNumber(value)) out[String(key)] = value;
    else if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) {
      out[String(key)] = Number(value);
    }
    // Non-numeric entries are dropped, not coerced to an invented 0.
  }
  return out;
}

export function normalizeReport(input) {
  if (!input) return null;
  if (!isObject(input)) return null;
  const findings = Array.isArray(input.findings)
    ? input.findings.map(normalizeFinding).filter(Boolean)
    : [];
  const report = {
    verdict: input.verdict !== undefined && input.verdict !== null
      ? String(input.verdict).toUpperCase()
      : '',
    findings,
    node_voltages: normalizeNumberMap(
      input.node_voltages ?? input.voltages ?? {},
    ),
    branch_currents: normalizeNumberMap(
      input.branch_currents ?? input.currents ?? {},
    ),
    power: normalizeNumberMap(
      input.power ?? input.component_power ?? input.per_component_power ?? {},
    ),
    uncertain: Array.isArray(input.uncertain)
      ? input.uncertain.map((u) => String(u))
      : [],
  };
  // Keep the netlist the report was computed from, if the caller gave one
  // (overlay.js reads netlist_used.components for node names; a plain
  // `netlist` array is its other accepted shape).
  if (input.netlist_used) report.netlist_used = input.netlist_used;
  if (Array.isArray(input.netlist)) report.netlist = input.netlist;
  return report;
}

function normalizeChange(raw) {
  if (typeof raw === 'string') return raw;
  if (isObject(raw)) {
    const text = raw.description ?? raw.message ?? raw.text ?? raw.summary;
    if (typeof text === 'string' && text) return text;
    if (raw.id && raw.value != null) return String(raw.id) + ': ' + String(raw.value);
    return '';
  }
  return '';
}

function normalizeHistoryEntry(raw) {
  if (typeof raw === 'string') {
    return { round: undefined, netlist: null, report: null, changes: [raw] };
  }
  if (!isObject(raw)) return null;
  const rawChanges = raw.changes ?? raw.what_changed ?? raw.summary ?? [];
  const changes = Array.isArray(rawChanges)
    ? rawChanges.map(normalizeChange).filter((s) => s !== '')
    : (typeof rawChanges === 'string' && rawChanges ? [rawChanges] : []);
  const round = raw.round ?? raw.round_number;
  return {
    // Round is kept exactly as supplied; history.js falls back to the
    // 1-based entry index when it is absent. main3d.js invents none.
    round: isFiniteNumber(round) ? round : (round !== undefined && round !== null && Number.isFinite(Number(round)) ? Number(round) : undefined),
    // Raw netlist/report pass through untouched for onShowRound, which
    // history.js hands back exactly as supplied.
    netlist: raw.netlist != null ? raw.netlist : null,
    report: isObject(raw.report) ? raw.report
      : (isObject(raw.result) ? raw.result
      : ((raw.verdict != null || Array.isArray(raw.findings))
        ? { verdict: raw.verdict, findings: raw.findings }
        : null)),
    changes,
  };
}

export function normalizeHistory(input) {
  if (!input) return [];
  let list = null;
  if (Array.isArray(input)) list = input;
  else if (isObject(input) && Array.isArray(input.history)) list = input.history;
  else if (isObject(input)) list = [input];
  if (!Array.isArray(list)) return [];
  const out = [];
  for (const raw of list) {
    const entry = normalizeHistoryEntry(raw);
    if (entry) out.push(entry);
  }
  return out;
}

// ---------------------------------------------------------------------------
// init3D
// ---------------------------------------------------------------------------

/**
 * Create the 3D view inside `container` and wire it to the page.
 *
 * @param {HTMLElement|string} container - element, element id, or selector.
 * @param {object} [callbacks] - page callbacks, optionally also carrying
 *   the initial netlist / report / history:
 *   onSelect(componentId | null, component | null),
 *   onMove(componentId, pose {x,y,z}, component),
 *   onEdit(componentId, updatedRow, netlist),
 *   onNetlistChange(netlist),
 *   onRecheck(editedRow, netlist), onRepair(editedRow, netlist)
 *     (only passed to the edit panel when supplied),
 *   onShowRound(roundIndex, netlist, report),
 *   onDragStart(componentId, component), onDragEnd(componentId, pose, component),
 *   onEditClose(componentId), onError(error),
 *   editOnSelect (default false): open the edit panel on click-select
 *     instead of on double-click only.
 *   netlist?, report?, history? (initial data, same shapes as the
 *   setNetlist / updateReport / renderHistory arguments).
 * @param {object} [options] - { netlist?, report?, history?,
 *   historyContainer? (element or selector for the repair-history
 *   panel; defaults to the #repair-history list's parent section,
 *   else the panel is left unmounted and exposed as
 *   handle.historyElement) }.
 *   Data in `options` wins over the same field in `callbacks`.
 * @returns {object} handle with setNetlist, updateReport/setReport,
 *   renderHistory, appendHistory, showRound, clearHistoryView, select,
 *   clearSelection, edit, getSelected, getNetlist, getReport,
 *   getHistory, resetView, setExploded, toggleExplode, isExploded,
 *   dispose/destroy, plus view/scene/camera/renderer/
 *   controls/historyElement.
 */
export function init3D(container, callbacks = {}, options = {}) {
  const pageCallbacks = isObject(callbacks) ? callbacks : {};
  const opts = isObject(options) ? options : {};

  const element = typeof container === 'string'
    ? document.getElementById(container) ?? document.querySelector(container)
    : container;
  if (!element || typeof element.appendChild !== 'function') {
    throw new TypeError(
      'netproof 3D: init3D needs a container element (or its id / selector)',
    );
  }

  const onError = typeof pageCallbacks.onError === 'function'
    ? pageCallbacks.onError
    : (error) => { console.error('[netproof 3D]', error); };
  const editOnSelect = pageCallbacks.editOnSelect === true;

  // -- State: everything here comes from arguments, nothing is invented. --
  // current* = the page's working netlist/report (what /check posts).
  // displayed* = what the scene currently shows (== current*, unless a
  // repair round is being viewed — see the header note).
  let currentNetlist = [];
  let currentReport = null;
  let displayedNetlist = [];
  let displayedReport = null;
  let viewingHistory = false;
  let repairHistoryEntries = [];
  let selectedId = null;
  let disposed = false;
  let rafId = 0;
  let lastFrameTime = 0;

  // -- Blueprint Inspect state (spec §8): visual only, never touches
  // -- netlist / verdict. exploded + the active tween live here.
  let exploded = false;
  let explodeTween = null; // { entries: [{group, fromY, toY}], start, duration } | null

  // -- Blueprint callouts (spec §9): guarded dynamic import of
  // -- ./callouts.js. Null until (and unless) that module lands.
  let calloutsHandle = null; // adapted { update, recompute, dispose } | null
  let calloutsLoading = false;

  // -- Blueprint buttons + R key (spec §2.4, §8, §10): listeners
  // -- attached by wireBlueprintButtons(), removed on dispose.
  let buttonListeners = []; // [{ el, type, handler }]
  let explodeButton = null;
  let resetButton = null;

  // -- Scene (./scene.js). Owns the render loop + resize. ------------------
  const view = createScene(element);
  const scene = view.scene;
  const camera = view.camera;
  const renderer = view.renderer;
  const controls = view.controls;
  if (!scene || !camera || !renderer) {
    throw new Error('netproof 3D: scene.js createScene did not return a scene, camera and renderer');
  }

  // Orbit / pan / zoom (spec §2.2): OrbitControls lives in scene.js
  // and keeps its bindings (left-drag rotate, right-drag pan, wheel
  // zoom). Blueprint limits only apply to an orthographic camera:
  // wheel zoom changes camera.zoom there, not distance, so the
  // perspective min/maxDistance limits are meaningless and must not
  // clamp the fixed ortho radius — neutralise them. A perspective
  // camera (scene.js before its ortho switch) keeps its own limits.
  if (camera.isOrthographicCamera) {
    controls.minZoom = 0.5;
    controls.maxZoom = 8;
    controls.minPolarAngle = 0.15;
    controls.maxPolarAngle = Math.PI / 2 - 0.02; // never below the slab
    controls.screenSpacePanning = false; // pan in the ground plane
    controls.minDistance = 0;
    controls.maxDistance = Infinity;
    if (typeof controls.update === 'function') controls.update();
  }

  // The container ships with a loading placeholder (index.html
  // .fallback). Once the scene exists the placeholder must go: left in
  // place its text sits over the canvas and the panel looks broken
  // even though the view works. A later throw still lands in the
  // page's failure path, which rewrites the container itself.
  const placeholder = element.querySelector('.fallback');
  if (placeholder) placeholder.remove();

  // -- THREE as a global, for history.js only (UMD: reads window.THREE). ---
  const prevWindowTHREE = (typeof window !== 'undefined') ? window.THREE : undefined;
  let installedWindowTHREE = false;
  if (typeof window !== 'undefined' && !window.THREE) {
    window.THREE = THREE;
    installedWindowTHREE = true;
  }

  // -- Overlays (./overlay.js). Label layer lives inside the container, --
  // -- so the container must be a positioned ancestor. ---------------------
  if (typeof window !== 'undefined' && window.getComputedStyle) {
    try {
      if (window.getComputedStyle(element).position === 'static') {
        element.style.position = 'relative';
      }
    } catch (error) { /* non-fatal; labels may misplace without it */ }
  }
  initOverlays({ THREE, scene, camera, container: element });

  // -- Circuit (./components.js). ------------------------------------------
  let circuit = null; // { group, nodePositions, meshesById: Map } | null
  let interactionHandle = null;

  function meshesObject() {
    if (!circuit) return {};
    return Object.fromEntries(circuit.meshesById);
  }

  function applyOverlays() {
    if (!displayedReport) return;
    try {
      const nodesById = {};
      for (const row of displayedNetlist) nodesById[row.id] = row.nodes.slice();
      updateOverlays(displayedReport, meshesObject(), { nodesById });
    } catch (error) {
      onError(error);
    }
    updateCalloutsNow();
  }

  // -- Blueprint camera reset (spec §2.4) ---------------------------------
  // Snap back to the canonical iso direction and fit the bounding box.
  // Prefers scene.js's view.resetToIso() when it exists; otherwise set
  // the iso direction ourselves and let frameCamera() (which keeps the
  // current direction) do the fit. Works with an empty scene too: the
  // direction is still reset, frameCamera just has nothing to fit.
  function resetViewInternal() {
    if (disposed) return false;
    try {
      if (view && typeof view.resetToIso === 'function') {
        const result = view.resetToIso();
        return result !== false;
      }
    } catch (error) {
      onError(error);
      return false;
    }
    try {
      const target = controls.target.clone();
      camera.position.copy(target).addScaledVector(
        new THREE.Vector3(ISO_DIRECTION.x, ISO_DIRECTION.y, ISO_DIRECTION.z),
        50,
      );
      if (typeof controls.update === 'function') controls.update();
      return frameCamera(view);
    } catch (error) {
      onError(error);
      return false;
    }
  }

  // -- Blueprint Inspect explode (spec §8) --------------------------------
  // Visual only: writes group.position.y and nothing else. Never calls
  // onMove / onNetlistChange / mergeEdit; wires, node dots, slab and
  // dimension lines stay on the ground. Labels/callouts follow because
  // they re-project from the live bounding box every frame. The tween
  // itself lives in scene.js (view.setExploded, driven by its own
  // animate loop); the local tween below is only a fallback for a
  // scene module that predates view.setExploded.
  function syncExplodeButton() {
    if (!explodeButton) return;
    try {
      explodeButton.setAttribute('aria-pressed', exploded ? 'true' : 'false');
    } catch (error) { /* button state is cosmetic; never fatal */ }
  }

  function resetExplodeState() {
    explodeTween = null;
    if (exploded) {
      exploded = false;
      syncExplodeButton();
    }
  }

  function setExplodedInternal(value) {
    if (disposed) return false;
    const next = value === true;
    // Delegate to the view when it owns explode (scene.js): its own
    // animate loop drives the tween (same 0.9 + 0.18 * index offsets),
    // so main3d.js must not run a second tween on the same groups.
    // The local tween below runs only when the scene module predates
    // view.setExploded.
    if (view && typeof view.setExploded === 'function') {
      try {
        explodeTween = null;
        exploded = view.setExploded(next) === true;
        syncExplodeButton();
        return exploded;
      } catch (error) {
        onError(error);
        // Fall through to the local tween below.
      }
    }
    const groups = [];
    displayedNetlist.forEach((row, index) => {
      const group = circuit && circuit.meshesById
        ? circuit.meshesById.get(row.id)
        : undefined;
      if (!group) return;
      if (group.userData.baseY === undefined) group.userData.baseY = group.position.y;
      groups.push({
        group,
        fromY: group.position.y,
        toY: group.userData.baseY + (next ? EXPLODE_BASE_OFFSET + EXPLODE_STEP_OFFSET * index : 0),
      });
    });
    exploded = next;
    syncExplodeButton();
    if (prefersReducedMotion()) {
      // Reduced motion: no 300 ms tween, set the end Y immediately.
      explodeTween = null;
      for (const entry of groups) entry.group.position.y = entry.toY;
      return exploded;
    }
    if (!groups.length) {
      explodeTween = null;
      return exploded;
    }
    // Toggling mid-tween reverses from the current Y: fromY is read
    // live above, never snapped to an endpoint first.
    explodeTween = {
      entries: groups,
      start: (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now(),
      duration: EXPLODE_DURATION_MS,
    };
    return exploded;
  }

  function stepExplodeTween(now) {
    if (!explodeTween) return;
    const elapsed = now - explodeTween.start;
    const t = Math.min(1, Math.max(0, elapsed / explodeTween.duration));
    const eased = easeInOutCubic(t);
    for (const entry of explodeTween.entries) {
      entry.group.position.y = entry.fromY + (entry.toY - entry.fromY) * eased;
    }
    if (t >= 1) {
      for (const entry of explodeTween.entries) entry.group.position.y = entry.toY;
      explodeTween = null;
    }
  }

  // -- Blueprint callouts (spec §9) ----------------------------------------
  // Guarded dynamic import: callouts.js may not exist yet (another
  // agent owns it). A failed import is silently skipped — the 3D view
  // works without callouts and this is never surfaced as a 3D error.
  function adaptCalloutsModule(mod) {
    if (!mod) return null;
    const init = mod.initCallouts || mod.default || mod.createCallouts;
    if (typeof init !== 'function') return null;
    let raw = null;
    try {
      raw = init({ THREE, scene, camera, container: element });
    } catch (error) {
      return null; // callout init failure must not break the 3D view
    }
    const handle = raw || {};
    const updateFn = handle.updateCallouts || handle.update
      || mod.updateCallouts || mod.update;
    const recomputeFn = handle.layoutCallouts || handle.updatePositions
      || handle.recompute || handle.layout || handle.update
      || mod.layoutCallouts || mod.updatePositions || mod.recompute
      || mod.layout;
    const disposeFn = handle.dispose || handle.destroy
      || mod.disposeCallouts || mod.dispose;
    return {
      update(report, meshes, extra) {
        if (typeof updateFn === 'function') updateFn.call(handle, report, meshes, extra);
      },
      recompute() {
        if (typeof recomputeFn === 'function') recomputeFn.call(handle);
      },
      dispose() {
        if (typeof disposeFn === 'function') disposeFn.call(handle);
      },
    };
  }

  function loadCallouts() {
    if (calloutsLoading || calloutsHandle) return;
    calloutsLoading = true;
    import('./callouts.js').then((mod) => {
      if (disposed) return;
      calloutsHandle = adaptCalloutsModule(mod);
      if (calloutsHandle) updateCalloutsNow();
    }).catch(() => {
      // callouts.js not landed yet: 3D works without it. No onError —
      // a missing optional module is not a 3D failure and must not
      // put real-error text (or any error) in #scene3d-status.
      calloutsHandle = null;
    });
  }

  function updateCalloutsNow() {
    if (!calloutsHandle) return;
    try {
      calloutsHandle.update(displayedReport, meshesObject(), { selectedId });
    } catch (error) {
      onError(error);
    }
  }

  function recomputeCallouts() {
    if (!calloutsHandle) return;
    try {
      calloutsHandle.recompute();
    } catch (error) {
      onError(error);
    }
  }

  // -- Blueprint buttons + R key (spec §2.4, §8, §10) ----------------------
  // The two buttons live in #scene3d-section in the host page (owned by
  // index.html). They may not exist yet when init3D runs, so lookup is
  // tolerant: known ids first, then a text match inside the section.
  // The handle methods (resetView / toggleExplode) are the primary
  // wiring — the page wires the same buttons after netproof:3d-ready —
  // this is the in-module wiring for buttons already in the DOM.
  function findBlueprintButton(predicate) {
    const ids = ['reset-view', 'scene3d-reset-view', 'reset-view-btn',
      'scene3d-reset', 'inspect-explode', 'explode-toggle',
      'scene3d-explode', 'inspect-toggle'];
    for (const id of ids) {
      const el = (typeof document !== 'undefined') ? document.getElementById(id) : null;
      if (el && predicate(el)) return el;
    }
    const section = (typeof document !== 'undefined')
      ? (document.getElementById('scene3d-section')
        || (element.closest ? element.closest('section') : null)
        || element.parentElement)
      : null;
    if (section && typeof section.querySelectorAll === 'function') {
      const buttons = section.querySelectorAll('button');
      for (const btn of buttons) {
        if (predicate(btn)) return btn;
      }
    }
    return null;
  }

  function wireBlueprintButtons() {
    if (typeof document === 'undefined') return;
    const isReset = (el) => /reset\s*view/i.test(el.textContent || '')
      || /reset/i.test(el.id || '');
    const isExplode = (el) => /inspect/i.test(el.textContent || '')
      || el.getAttribute('aria-pressed') !== null
      || /explode/i.test(el.id || '');
    const resetBtn = findBlueprintButton(isReset);
    if (resetBtn && resetBtn !== explodeButton) {
      const onReset = () => { resetViewInternal(); };
      resetBtn.addEventListener('click', onReset);
      buttonListeners.push({ el: resetBtn, type: 'click', handler: onReset });
      try { resetBtn.disabled = false; } catch (error) { /* cosmetic */ }
      resetButton = resetBtn;
    }
    const explodeBtn = findBlueprintButton(isExplode);
    if (explodeBtn && explodeBtn !== resetBtn) {
      const onExplode = () => { handle.toggleExplode(); };
      explodeBtn.addEventListener('click', onExplode);
      buttonListeners.push({ el: explodeBtn, type: 'click', handler: onExplode });
      try { explodeBtn.disabled = false; } catch (error) { /* cosmetic */ }
      explodeButton = explodeBtn;
      syncExplodeButton();
    }
  }

  function onContainerKeydown(event) {
    // R resets the iso view when #scene3d itself has focus, alongside
    // the existing arrow-key selection (page fallback) and the
    // interaction.js canvas handler. Only fires when the container is
    // the event target, so typing elsewhere is never hijacked.
    if (!event || event.target !== element) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === 'r' || event.key === 'R') {
      event.preventDefault();
      resetViewInternal();
    }
  }

  function disposeGroup(group) {
    group.traverse((child) => {
      if (child.isMesh) {
        if (child.geometry && typeof child.geometry.dispose === 'function') {
          child.geometry.dispose();
        }
        const materials = Array.isArray(child.material) ? child.material : [child.material];
        for (const material of materials) {
          if (material && typeof material.dispose === 'function') material.dispose();
        }
      }
    });
  }

  function clearCircuit() {
    if (!circuit) return;
    for (const id of circuit.meshesById.keys()) view.components.delete(id);
    scene.remove(circuit.group);
    disposeGroup(circuit.group);
    circuit = null;
  }

  function buildDisplayed() {
    // A rebuild is a new drawing: never carry an exploded state across
    // it (spec §8). Cancels any mid-tween and resets aria-pressed, and
    // snaps the view's own explode state back too (view.resetExplode).
    resetExplodeState();
    if (view && typeof view.resetExplode === 'function') {
      try { view.resetExplode(); } catch (error) { onError(error); }
    }
    clearCircuit();
    if (!displayedNetlist.length) {
      if (interactionHandle && typeof interactionHandle.setMeshes === 'function') {
        interactionHandle.setMeshes(new Map());
      }
      try { setWires({}); } catch (error) { onError(error); }
      applyOverlays();
      updateCalloutsNow(); // no Report yet: a no-op by design
      // (updateCallouts early-returns; no verdict/findings exist
      // before a check). Per-part id/type/value still show via the
      // click-to-open detail callout, which reads mesh userData.
      return;
    }
    let built;
    try {
      built = buildCircuit(displayedNetlist);
    } catch (error) {
      onError(error);
      return;
    }
    circuit = built;
    scene.add(built.group);
    // Register in scene.js's component map so its raycast picking finds
    // these meshes (buildCircuit groups are children of circuit.group,
    // not of the scene, so addComponent's registration does not apply).
    for (const [id, mesh] of built.meshesById) view.components.set(id, mesh);
    // Store each group's base Y now (today 0 for every group) so the
    // explode tween always has a real rest position to return to.
    for (const group of built.meshesById.values()) {
      if (group && group.userData && group.userData.baseY === undefined) {
        group.userData.baseY = group.position.y;
      }
    }
    // Reset to the canonical iso view via view.resetToIso() (load,
    // every setNetlist, and every history-round / edit rebuild all
    // come through here). Never frameCamera() here: a rebuild is a
    // new drawing, so the orbit is discarded, not kept. No-op when
    // there is nothing to frame. A plain updateReport never comes
    // through here, so it never resets the camera.
    try {
      if (view && typeof view.resetToIso === 'function') view.resetToIso();
      else resetViewInternal(); // scene.js predating resetToIso
    } catch (error) { onError(error); }
    if (selectedId && built.meshesById.has(selectedId)) {
      try { view.setSelected(selectedId); } catch (error) { /* highlight-only */ }
    }
    if (interactionHandle && typeof interactionHandle.setMeshes === 'function') {
      interactionHandle.setMeshes(built.meshesById);
    }
    // Flow dots ride the orthogonal route: built.wirePointsById is the
    // elbow-following polyline per component id (buildCircuit /
    // buildWires), not a straight node-to-node line, so the dots
    // follow the same corners the wire ribbons do.
    try { setWires(built.wirePointsById ?? {}); } catch (error) { onError(error); }
    applyOverlays();
    updateCalloutsNow();
  }

  // -- Edit panel (./edit-panel.js). ----------------------------------------
  function mergeEdit(oldId, editedRow) {
    const updated = normalizeComponent(editedRow);
    if (!updated) {
      onError(new Error(
        'netproof 3D: edit panel returned a row without an id or without ' +
        'exactly two distinct nodes (component ' + oldId + ')',
      ));
      return null;
    }
    if (updated.id !== oldId && currentNetlist.some((row) => row.id === updated.id)) {
      onError(new Error(
        'netproof 3D: cannot rename ' + oldId + ' to ' + updated.id +
        ': that id already exists in the netlist',
      ));
      return null;
    }
    if (selectedId === oldId) selectedId = updated.id;
    currentNetlist = currentNetlist.map((row) => (row.id === oldId ? updated : row));
    displayedNetlist = currentNetlist;
    viewingHistory = false;
    return updated;
  }

  function openEditorFor(componentId) {
    if (viewingHistory) {
      // Editing a historical round would corrupt the working netlist:
      // return to the working netlist first, then edit that component.
      viewingHistory = false;
      displayedNetlist = currentNetlist;
      displayedReport = currentReport;
      buildDisplayed();
    }
    const component = currentNetlist.find((row) => row.id === componentId);
    if (!component) return false;
    const panelCallbacks = {
      onSave: (editedRow) => {
        const updated = mergeEdit(componentId, editedRow);
        if (!updated) return;
        buildDisplayed();
        if (typeof pageCallbacks.onEdit === 'function') {
          pageCallbacks.onEdit(componentId, { ...updated }, currentNetlist.map((r) => ({ ...r })));
        }
        if (typeof pageCallbacks.onNetlistChange === 'function') {
          pageCallbacks.onNetlistChange(currentNetlist.map((r) => ({ ...r })));
        }
      },
      onClose: () => {
        if (typeof pageCallbacks.onEditClose === 'function') {
          pageCallbacks.onEditClose(componentId);
        }
      },
    };
    if (typeof pageCallbacks.onRecheck === 'function') {
      panelCallbacks.onRecheck = (editedRow) => {
        const updated = mergeEdit(componentId, editedRow);
        if (!updated) return;
        buildDisplayed();
        pageCallbacks.onRecheck({ ...updated }, currentNetlist.map((r) => ({ ...r })));
      };
    }
    if (typeof pageCallbacks.onRepair === 'function') {
      panelCallbacks.onRepair = (editedRow) => {
        const updated = mergeEdit(componentId, editedRow);
        if (!updated) return;
        buildDisplayed();
        pageCallbacks.onRepair({ ...updated }, currentNetlist.map((r) => ({ ...r })));
      };
    }
    try {
      showEditPanel({ ...component }, currentReport, panelCallbacks);
      return true;
    } catch (error) {
      onError(error);
      return false;
    }
  }

  // -- Interaction (./interaction.js): select / move / edit -> page. -------
  function componentById(id) {
    const row = currentNetlist.find((r) => r.id === id);
    return row ? { ...row } : null;
  }

  function handleSelect(componentId, event) {
    if (disposed) return;
    if (componentId === undefined || componentId === null) {
      selectedId = null;
      try { view.setSelected(null); } catch (error) { /* highlight-only */ }
      updateCalloutsNow();
      try {
        if (typeof pageCallbacks.onSelect === 'function') pageCallbacks.onSelect(null, null, event);
      } catch (error) { onError(error); }
      return;
    }
    const id = String(componentId);
    selectedId = id;
    try { view.setSelected(id); } catch (error) { /* highlight-only */ }
    updateCalloutsNow(); // selected callout may become interactive
    try {
      if (typeof pageCallbacks.onSelect === 'function') {
        pageCallbacks.onSelect(id, componentById(id), event);
      }
    } catch (error) { onError(error); }
    if (editOnSelect) openEditorFor(id);
  }

  function handleMove(componentId, pose, event) {
    if (disposed || componentId === undefined || componentId === null) return;
    const id = String(componentId);
    try {
      if (typeof pageCallbacks.onMove === 'function') {
        // pose comes from the interaction module (layout only); moving a
        // component never changes its nodes/value, so this is not a
        // netlist change and onNetlistChange is deliberately not called.
        pageCallbacks.onMove(id, pose, componentById(id), event);
      }
    } catch (error) { onError(error); }
  }

  function handleEdit(componentId) {
    if (disposed || componentId === undefined || componentId === null) return;
    openEditorFor(String(componentId));
  }

  function handleDragStart(componentId) {
    if (disposed || typeof pageCallbacks.onDragStart !== 'function') return;
    try { pageCallbacks.onDragStart(String(componentId), componentById(String(componentId))); }
    catch (error) { onError(error); }
  }

  function handleDragEnd(componentId, pose) {
    if (disposed || typeof pageCallbacks.onDragEnd !== 'function') return;
    try { pageCallbacks.onDragEnd(String(componentId), pose, componentById(String(componentId))); }
    catch (error) { onError(error); }
  }

  // -- Initial netlist, then interaction (it needs the meshes). ------------
  currentNetlist = normalizeNetlist(opts.netlist ?? pageCallbacks.netlist ?? []);
  displayedNetlist = currentNetlist;
  const initialReport = opts.report ?? pageCallbacks.report ?? null;
  if (initialReport) {
    const normalized = normalizeReport(initialReport);
    if (normalized) {
      currentReport = normalized;
      displayedReport = normalized;
      if (currentNetlist.length === 0 && (normalized.netlist_used || normalized.netlist)) {
        const fromReport = normalizeNetlist(normalized.netlist_used ?? normalized.netlist);
        if (fromReport.length > 0) {
          currentNetlist = fromReport;
          displayedNetlist = fromReport;
        }
      }
    }
  }
  buildDisplayed();

  interactionHandle = setupInteraction(
    scene,
    camera,
    renderer,
    circuit ? circuit.meshesById : new Map(),
    {
      onSelect: handleSelect,
      onMove: handleMove,
      onEdit: handleEdit,
      onDragStart: handleDragStart,
      onDragEnd: handleDragEnd,
    },
  );
  if (interactionHandle && typeof interactionHandle.setControls === 'function') {
    interactionHandle.setControls(controls);
  }

  // -- Repair history (./history.js, via window.NetproofHistory). -----------
  const HistoryNS = (typeof globalThis !== 'undefined')
    ? globalThis.NetproofHistory
    : undefined;
  let historyHandle = null;
  let historySceneAttached = false;

  function resolveHistoryMount() {
    if (opts.historyContainer) {
      if (typeof opts.historyContainer === 'string') {
        return document.querySelector(opts.historyContainer);
      }
      return opts.historyContainer;
    }
    const list = document.getElementById('repair-history');
    if (list && list.parentElement) return list.parentElement;
    return null;
  }

  function handleShowRound(roundIndex, rawNetlist, rawReport) {
    if (disposed) return;
    displayedNetlist = normalizeNetlist(rawNetlist);
    displayedReport = normalizeReport(rawReport);
    viewingHistory = true;
    buildDisplayed();
    try {
      if (typeof pageCallbacks.onShowRound === 'function') {
        pageCallbacks.onShowRound(roundIndex, rawNetlist, rawReport);
      }
    } catch (error) { onError(error); }
  }

  function ensureHistoryScene() {
    if (!historyHandle || historySceneAttached) return;
    if (!repairHistoryEntries.length) return;
    try {
      const attached = historyHandle.attachScene({ scene, camera, renderer });
      if (attached !== false) historySceneAttached = true;
    } catch (error) {
      onError(error);
    }
  }

  function createOrUpdateHistory() {
    if (!HistoryNS || typeof HistoryNS.renderHistory !== 'function') {
      onError(new Error(
        'netproof 3D: history.js did not register window.NetproofHistory; ' +
        'repair history will not render',
      ));
      return;
    }
    if (!historyHandle) {
      const mount = resolveHistoryMount();
      try {
        historyHandle = HistoryNS.renderHistory(
          repairHistoryEntries,
          { onShowRound: handleShowRound },
          mount ?? undefined,
        );
      } catch (error) {
        onError(error);
        return;
      }
      ensureHistoryScene();
    } else {
      try {
        historyHandle.update(repairHistoryEntries);
      } catch (error) {
        onError(error);
        return;
      }
      ensureHistoryScene();
    }
  }

  repairHistoryEntries = normalizeHistory(opts.history ?? pageCallbacks.history ?? []);
  createOrUpdateHistory();

  // -- Animation loop: overlay only. scene.js owns render + controls. ------
  function tick(time) {
    if (disposed) return;
    rafId = requestAnimationFrame(tick);
    const now = typeof time === 'number' ? time : performance.now();
    const deltaSeconds = lastFrameTime ? Math.max(0, (now - lastFrameTime) / 1000) : 0;
    lastFrameTime = now;
    try {
      const currents = displayedReport ? displayedReport.branch_currents : {};
      animateFlow(deltaSeconds, currents || {});
      updateLabelPositions(camera);
      // Blueprint tick order (spec §10): labels first, then callout
      // recompute (leader lines track rising parts every frame during
      // the explode tween), then the explode tween step.
      recomputeCallouts();
      stepExplodeTween(now);
    } catch (error) {
      onError(error);
    }
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    cancelAnimationFrame(rafId);
    explodeTween = null;
    try {
      for (const { el, type, handler } of buttonListeners) {
        el.removeEventListener(type, handler);
      }
    } catch (error) { /* listener removal is best-effort */ }
    buttonListeners = [];
    explodeButton = null;
    resetButton = null;
    try {
      element.removeEventListener('keydown', onContainerKeydown);
    } catch (error) { /* listener removal is best-effort */ }
    try {
      if (calloutsHandle && typeof calloutsHandle.dispose === 'function') {
        calloutsHandle.dispose();
      }
    } catch (error) { onError(error); }
    calloutsHandle = null;
    try {
      if (interactionHandle && typeof interactionHandle.dispose === 'function') {
        interactionHandle.dispose();
      }
    } catch (error) { onError(error); }
    try { disposeOverlays(); } catch (error) { onError(error); }
    try {
      if (historyHandle && typeof historyHandle.destroy === 'function') {
        historyHandle.destroy();
      }
    } catch (error) { onError(error); }
    try { hideEditPanel(); } catch (error) { /* panel may never have opened */ }
    clearCircuit();
    try { view.dispose(); } catch (error) { onError(error); }
    if (installedWindowTHREE && typeof window !== 'undefined' && window.THREE === THREE) {
      if (prevWindowTHREE === undefined) delete window.THREE;
      else window.THREE = prevWindowTHREE;
    }
    historyHandle = null;
    interactionHandle = null;
  }

  // -- Handle: the page drives netlist / report / history through these. --
  const handle = {
    container: element,
    view,
    scene,
    camera,
    renderer,
    controls,
    THREE,
    historyElement: historyHandle ? historyHandle.element : null,

    getNetlist() {
      return currentNetlist.map((row) => ({ ...row }));
    },

    getReport() {
      return currentReport;
    },

    getHistory() {
      return repairHistoryEntries.map((entry) => ({ ...entry }));
    },

    getSelected() {
      return selectedId;
    },

    isShowingHistoryRound() {
      return viewingHistory;
    },

    /** Replace the working netlist (any accepted shape) and rebuild. */
    setNetlist(netlist, report) {
      if (disposed) return handle;
      currentNetlist = normalizeNetlist(netlist);
      displayedNetlist = currentNetlist;
      viewingHistory = false;
      if (report !== undefined) {
        currentReport = normalizeReport(report);
        displayedReport = currentReport;
      }
      buildDisplayed();
      return handle;
    },

    /**
     * Update overlays from a Report (any accepted shape). If the report
     * carries netlist_used / netlist and no netlist has been set yet, the
     * circuit is built from that netlist first (values from the
     * argument only). A fresh report always returns the scene from a
     * viewed history round to the working netlist.
     */
    updateReport(report) {
      if (disposed) return handle;
      const normalized = normalizeReport(report);
      if (!normalized) return handle;
      currentReport = normalized;
      const wasViewingHistory = viewingHistory;
      viewingHistory = false;
      displayedNetlist = currentNetlist;
      displayedReport = normalized;
      let needsRebuild = wasViewingHistory;
      if (currentNetlist.length === 0 && (normalized.netlist_used || normalized.netlist)) {
        const fromReport = normalizeNetlist(normalized.netlist_used ?? normalized.netlist);
        if (fromReport.length > 0) {
          currentNetlist = fromReport;
          displayedNetlist = fromReport;
          needsRebuild = true;
        }
      }
      if (needsRebuild) buildDisplayed();
      else applyOverlays();
      return handle;
    },

    /** Alias for updateReport. */
    setReport(report) {
      return handle.updateReport(report);
    },

    /** Replace the repair history (any accepted shape) and render it. */
    renderHistory(history) {
      if (disposed) return handle;
      repairHistoryEntries = normalizeHistory(history);
      createOrUpdateHistory();
      return handle;
    },

    /** Append one repair entry and re-render. */
    appendHistory(entry) {
      if (disposed) return handle;
      const normalized = normalizeHistoryEntry(entry);
      if (normalized) {
        repairHistoryEntries = repairHistoryEntries.concat([normalized]);
        createOrUpdateHistory();
      }
      return handle;
    },

    /** Show one repair round (0-based index into the history array). */
    showRound(index) {
      if (disposed || !historyHandle) return handle;
      try { historyHandle.select(index, true); } catch (error) { onError(error); }
      return handle;
    },

    /** Return the scene from a viewed history round to the working netlist. */
    clearHistoryView() {
      if (disposed) return handle;
      if (!viewingHistory) return handle;
      viewingHistory = false;
      displayedNetlist = currentNetlist;
      displayedReport = currentReport;
      buildDisplayed();
      return handle;
    },

    /** Select a component by id (same path as a pointer click). */
    select(componentId) {
      if (disposed || componentId === undefined || componentId === null) return handle;
      const id = String(componentId);
      if (interactionHandle && typeof interactionHandle.select === 'function') {
        interactionHandle.select(id);
      } else {
        handleSelect(id);
      }
      return handle;
    },

    clearSelection() {
      if (disposed) return handle;
      if (interactionHandle && typeof interactionHandle.deselect === 'function') {
        interactionHandle.deselect();
      } else {
        handleSelect(null);
      }
      return handle;
    },

    /** Open the edit panel for a component (same as double-clicking it). */
    edit(componentId) {
      if (disposed || componentId === undefined || componentId === null) return handle;
      openEditorFor(String(componentId));
      return handle;
    },

    /**
     * No-op kept for API compatibility: scene.js owns resize via a
     * ResizeObserver on the container (plus a window resize listener),
     * so the view already follows container size changes.
     */
    resize() {
      return handle;
    },

    /**
     * Blueprint reset view (spec §2.4): snap the camera back to the
     * canonical iso direction (0.577, 0.577, 0.577) and refit the
     * bounding box. Same action as the Reset view button and the R key.
     * Delegates to view.resetToIso() via resetViewInternal().
     */
    resetView() {
      if (disposed) return handle;
      resetViewInternal();
      return handle;
    },

    /**
     * Blueprint Inspect explode (spec §8): true raises every component
     * group on a staggered 300 ms eased tween (visual only), false
     * returns them to their base Y. Reduced-motion is instant.
     * Delegates to view.setExploded() (scene.js owns the tween).
     */
    setExploded(value) {
      if (disposed) return handle;
      setExplodedInternal(value === true);
      return handle;
    },

    /**
     * Toggle the Inspect explode state; returns the handle. Delegates
     * to the view: the next state is read from view.isExploded(), so
     * a toggle can never invert a stale local flag.
     */
    toggleExplode() {
      if (disposed) return handle;
      let next = !exploded;
      if (view && typeof view.isExploded === 'function') {
        try { next = !view.isExploded(); } catch (error) { /* use local flag */ }
      }
      setExplodedInternal(next);
      return handle;
    },

    /** Whether the Inspect explode toggle is currently on. */
    isExploded() {
      return exploded;
    },

    dispose,
    destroy: dispose,
  };

  // -- Blueprint wiring: buttons + R key + callouts (spec §2.4, §8-§10).
  // -- The page also wires the same buttons after netproof:3d-ready;
  // -- both paths call the same handle methods, so double-wiring is
  // -- impossible (each button gets one listener here, at most).
  wireBlueprintButtons();
  element.addEventListener('keydown', onContainerKeydown);
  loadCallouts();

  // -- Animation loop (overlay tick; scene.js renders). ---------------------
  rafId = requestAnimationFrame(tick);

  // KiCad part models load async; redraw with them once they arrive.
  partModelsReady.then(() => { if (!disposed) buildDisplayed(); });
  return handle;
}

export default init3D;
