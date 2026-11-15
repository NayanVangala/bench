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
//         frameCamera, dispose }
//       frameCamera(view) fits the camera to the bounding box of
//       view.components; main3d.js calls it in buildDisplayed() so
//       the view reframes on load and on every setNetlist.
//       Runs its OWN animation loop (controls.update + render) and its
//       OWN resize handling (ResizeObserver). main3d.js therefore does
//       NOT render and does NOT resize; its loop only drives the
//       overlay (flow dots + label projection, see below).
//       main3d.js does not pass onPick: pointer selection callbacks
//       come from interaction.js. scene.js still does its scale
//       highlight on click; main3d.js keeps that in sync via
//       view.setSelected().
//     Component meshes built by components.js are registered in
//     view.components (id -> group) so scene.js raycast picking and
//     verdict-colour helpers can find them.
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
//   ./history.js — UMD, NOT an ES module: no exports. Importing it is a
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
 *   getHistory, dispose/destroy, plus view/scene/camera/renderer/
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

  // -- Scene (./scene.js). Owns the render loop + resize. ------------------
  const view = createScene(element);
  const scene = view.scene;
  const camera = view.camera;
  const renderer = view.renderer;
  const controls = view.controls;
  if (!scene || !camera || !renderer) {
    throw new Error('netproof 3D: scene.js createScene did not return a scene, camera and renderer');
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

  function wiresFromNodePositions(built) {
    const wires = {};
    for (const comp of displayedNetlist) {
      const p1 = built.nodePositions.get(comp.nodes[0]);
      const p2 = built.nodePositions.get(comp.nodes[1]);
      if (p1 && p2) wires[comp.id] = [p1.clone(), p2.clone()];
    }
    return wires;
  }

  function buildDisplayed() {
    clearCircuit();
    if (!displayedNetlist.length) {
      if (interactionHandle && typeof interactionHandle.setMeshes === 'function') {
        interactionHandle.setMeshes(new Map());
      }
      try { setWires({}); } catch (error) { onError(error); }
      applyOverlays();
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
    // Frame the camera on the new circuit's bounding box (load, every
    // setNetlist, and every history-round / edit rebuild all come
    // through here). No-op when there is nothing to frame; the user's
    // orbit angle is kept, only distance/target are reset.
    try { frameCamera(view); } catch (error) { onError(error); }
    if (selectedId && built.meshesById.has(selectedId)) {
      try { view.setSelected(selectedId); } catch (error) { /* highlight-only */ }
    }
    if (interactionHandle && typeof interactionHandle.setMeshes === 'function') {
      interactionHandle.setMeshes(built.meshesById);
    }
    try { setWires(wiresFromNodePositions(built)); } catch (error) { onError(error); }
    applyOverlays();
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
      try {
        if (typeof pageCallbacks.onSelect === 'function') pageCallbacks.onSelect(null, null, event);
      } catch (error) { onError(error); }
      return;
    }
    const id = String(componentId);
    selectedId = id;
    try { view.setSelected(id); } catch (error) { /* highlight-only */ }
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
    } catch (error) {
      onError(error);
    }
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    cancelAnimationFrame(rafId);
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

    dispose,
    destroy: dispose,
  };

  // -- Animation loop (overlay tick; scene.js renders). ---------------------
  rafId = requestAnimationFrame(tick);

  return handle;
}

export default init3D;
