/**
 * Netproof 3D interaction module — web/3d/interaction.js
 *
 * Blueprint rebuild (2026-11-15). Owns pointer AND keyboard interaction
 * for the Netproof 3D schematic view:
 *   - click a component mesh to select it (raycast + emissive highlight
 *     + selection outline)
 *   - drag a selected component on the ground plane to move it (node
 *     positions and wire tubes are updated in the scene). Dragging is
 *     layout-only: onMove never changes nodes / value / verdict, and
 *     the netlist table is the keyboard path to every value that
 *     matters, so drag has no keyboard equivalent.
 *   - double-click a component to open the edit panel (via callback)
 *   - hover a component to show a tooltip with its id / type / value
 *   - keyboard: arrows / Home / End cycle parts with wrap, Enter opens
 *     the edit panel, Escape deselects (see "Keyboard map" below)
 *
 * This module NEVER invents numbers. It displays only the component data
 * already attached to the meshes (see contract below) and it never fetches,
 * never POSTs, and never calls an LLM. Editing a value or a node's wiring
 * goes through the callbacks to the main page (web/index.html), which is
 * the only place that POSTs the edited netlist to /check (no LLM).
 *
 * Three.js is expected to resolve via the page's import map
 * (`import * as THREE from 'three'`). The scene-building module
 * (scene.js) owns creating meshes, wires, and the renderer loop; this
 * module only attaches pointer/keyboard handlers to the renderer's DOM
 * element and to the callout buttons inside its container.
 *
 * ---------------------------------------------------------------------------
 * Contract
 * ---------------------------------------------------------------------------
 * setupInteraction(scene, camera, renderer, meshesById, callbacks)
 *
 *   scene      THREE.Scene — traversed to find node anchors and wire tubes
 *              when a component moves (see "Wire / node update" below).
 *   camera     THREE.Camera used for raycasting.
 *   renderer   THREE.WebGLRenderer — pointer listeners attach to
 *              renderer.domElement; keyboard listeners attach to the
 *              canvas (programmatic focus only) and, by delegation, to
 *              the callout buttons inside the canvas's parent (#scene3d).
 *   meshesById Map<string, THREE.Object3D> or plain object
 *              { [componentId]: meshOrGroup }. A value may be a single Mesh
 *              or a Group; raycast hits on any descendant resolve to the
 *              owning component. Each mesh/group SHOULD carry its component
 *              data in userData so the tooltip can show it:
 *                userData.componentId (or userData.id) — string
 *                userData.type                          — string, e.g. "resistor"
 *                userData.value                         — shown verbatim
 *                userData.nodes                         — array of node names
 *              Any field that is absent is simply omitted from the tooltip;
 *              nothing is defaulted or computed here.
 *   callbacks  {
 *                onSelect(componentId | null),  // click select / empty-click deselect
 *                onMove(componentId, {x, y, z}), // live while dragging, plus a
 *                                                // final call on drag end if
 *                                                // the position changed since
 *                                                // the last live call
 *                onEdit(componentId),           // double-click / Enter: open edit panel
 *                onDragStart?(componentId),     // optional: e.g. disable OrbitControls
 *                onDragEnd?(componentId, {x, y, z}) // optional
 *              }
 *              onSelect / onMove / onEdit are optional in the sense that a
 *              missing callback is a no-op, but the main page MUST supply
 *              them for selection, moves, and edits to have any effect
 *              outside the 3D view.
 *
 * Returns a handle:
 *   {
 *     dispose(),               // remove listeners, tooltip, highlight
 *     select(componentId),      // select programmatically (fires onSelect)
 *     deselect(),               // clear selection (fires onSelect(null))
 *     getSelectedId(),          // string | null
 *     setMeshes(meshesById),    // replace the mesh map (meshes added later)
 *     setControls(controls),    // OrbitControls-like { enabled }; auto-disabled
 *                               // while dragging, restored on drag end
 *     refreshWires(componentId) // re-run the wire/node update for one component
 *                                     (optional 2nd/3rd args: newPosition, delta,
 *                                      used internally while dragging)
 *     focusCallout(componentId),// focus the callout button for a part (bool)
 *     cycleSelection(dir),      // step selection +1 / -1 with wrap (returns id)
 *   }
 *
 * ---------------------------------------------------------------------------
 * Keyboard map (blueprint a11y spec §4.2 / §5.3)
 * ---------------------------------------------------------------------------
 * Tab / Shift+Tab moves through the callouts in netlist (DOM) order —
 * one Tab stop per callout. Callouts are real <button> elements owned
 * by the overlay/callouts layer; this module never reorders them and
 * never assigns a positive tabindex.
 *
 * Canvas (renderer.domElement, when it holds focus — it is
 * tabindex="-1", programmatic focus only, never a Tab stop):
 *   ArrowRight / ArrowDown  next part, wraps
 *   ArrowLeft / ArrowUp     previous part, wraps
 *   Home                    first part
 *   End                     last part
 *   Enter                   edit the selected part (or select the first
 *                           part when nothing is selected)
 *   Escape                  deselect
 *   Space                   nothing (page scroll is left alone)
 *
 * Callout button (delegated keydown on the #scene3d container; the
 * container handler in index.html only fires when the container
 * itself is the target, so the two never double-step):
 *   ArrowRight / ArrowDown  next callout, wraps; focus moves and the
 *                           selection follows
 *   ArrowLeft / ArrowUp     previous callout, wraps
 *   Home / End              first / last callout
 *   Enter / Space           open the edit panel for that part
 *   Escape                  deselect and return focus to #scene3d
 *
 * Window: Escape deselects, except when focus is inside the edit
 * panel (the panel owns that Escape: it closes the dialog and returns
 * focus) or inside a text input / select / textarea (those own Escape
 * themselves). A defaultPrevented Escape is never re-handled here.
 *
 * NOT handled here, on purpose:
 *   - "R" / Reset view and the Inspect (explode) toggle: main3d.js
 *     owns the R key on the container and wires the Inspect / Reset
 *     view buttons. This module never claims those keys.
 *   - Arrow keys on the #scene3d container itself: index.html owns
 *     them (target === container guard). This module's handlers only
 *     fire when the target is the canvas or a callout.
 *   - Typing: no other key is hijacked; anything not listed above is
 *     ignored.
 *
 * Selection announcements: every settled selection change is written
 * (debounced 150 ms, so holding an arrow key does not spam) to
 * #selected-announcer when that live region exists (owned by
 * index.html). Nothing is announced when it does not exist.
 *
 * Focus ring: callout buttons get a visible #FFD166 ring (3px, 2px
 * offset, dark #0A2A5E keyline) from the small injected stylesheet
 * below — the blueprint focus colour, which reads on blueprint blue
 * where the page's #1D4ED8 ring is 1.50:1 and effectively invisible.
 * Callouts are also 44px minimum targets (a11y spec §3).
 *
 * Reduced motion: this module animates nothing (selection highlight
 * is static, the tooltip does not animate), so there is nothing to
 * disable; the injected stylesheet still kills callout transitions
 * under prefers-reduced-motion.
 *
 * Wire / node update (on drag):
 *   1. The dragged mesh/group itself is moved (x/z from the ground-plane
 *      hit, y preserved so the component keeps its height).
 *   2. mesh.userData.nodePositions, if present (object/array of Vector3-like
 *      positions), is shifted by the same delta.
 *   3. Scene objects with userData.nodeAnchorFor === componentId are shifted
 *      by the same delta (or placed at newPosition + userData.anchorOffset).
 *   4. Scene objects flagged as wires (userData.wire === true or
 *      userData.isWire === true) are refreshed for the moved component:
 *        - if userData.update / userData.refreshWire is a function, it is
 *          called as fn(componentId, {x, y, z}, delta) and owns the rebuild;
 *        - else if it is a THREE.Line whose userData.endpoints is an array
 *          of { componentId, offset: {x, y, z} }, its position attribute is
 *          recomputed from the current component positions.
 *      Wire modules that build TubeGeometry should therefore register one of
 *      those two hooks (update function or endpoints array); this module
 *      cannot rebuild a tube from a curve it does not own, and it will not
 *      guess at one.
 *
 * Dragging vs clicking: a pointer press that moves less than the click
 * threshold (6 px) is a click; beyond it, it is a drag. Clicking empty
 * space deselects. Escape deselects. Dragging uses the ground plane y = 0.
 */

import * as THREE from 'three';

const HIGHLIGHT_EMISSIVE = 0x1e6fff;
const HIGHLIGHT_EMISSIVE_INTENSITY = 0.55;
const OUTLINE_COLOR = 0x2684ff;
const CLICK_THRESHOLD_PX = 6;
const EPSILON = 1e-6;
const GROUND_Y = 0;
const ANNOUNCE_DEBOUNCE_MS = 150;
const CALLOUT_STYLE_ID = 'netproof-callout-focus-style';

function isPlainPosition(v) {
  return v != null && typeof v === 'object' &&
    typeof v.x === 'number' && typeof v.y === 'number' && typeof v.z === 'number';
}

function toPlainPosition(v) {
  return { x: v.x, y: v.y, z: v.z };
}

function positionsEqual(a, b) {
  return Math.abs(a.x - b.x) < EPSILON &&
    Math.abs(a.y - b.y) < EPSILON &&
    Math.abs(a.z - b.z) < EPSILON;
}

function escapeText(value) {
  return String(value == null ? '' : value);
}

/**
 * Visible keyboard focus for callout buttons, injected once. The
 * blueprint focus colour #FFD166 (6.97:1 on blueprint blue #0B3D91)
 * plus a dark keyline so the ring also reads where amber meets white.
 * 44px minimum target per a11y spec §3. No transition under reduced
 * motion. This only styles the callout buttons this module drives
 * keyboard for; the overlay layer owns the rest of callout layout.
 */
function injectCalloutFocusStyle() {
  if (typeof document === 'undefined') return null;
  if (document.getElementById(CALLOUT_STYLE_ID)) {
    return document.getElementById(CALLOUT_STYLE_ID);
  }
  const style = document.createElement('style');
  style.id = CALLOUT_STYLE_ID;
  style.textContent = [
    '.netproof-callout { min-height: 44px; min-width: 44px; }',
    '.netproof-callout:focus-visible {',
    '  outline: 3px solid #FFD166; outline-offset: 2px;',
    '  box-shadow: 0 0 0 4px #0A2A5E; border-radius: 4px; scroll-margin: 72px;',
    '}',
    '.netproof-callout:focus:not(:focus-visible) { outline: none; }',
    '@media (prefers-reduced-motion: reduce) {',
    '  .netproof-callout { transition: none !important; }',
    '}',
  ].join('\n');
  document.head.appendChild(style);
  return style;
}

export function setupInteraction(scene, camera, renderer, meshesById, callbacks) {
  if (!scene || !camera || !renderer || !renderer.domElement) {
    throw new Error('setupInteraction: scene, camera and renderer (with domElement) are required');
  }
  const cb = callbacks || {};
  const dom = renderer.domElement;
  const groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -GROUND_Y);
  const raycaster = new THREE.Raycaster();
  const pointerNdc = new THREE.Vector2();
  // The canvas is programmatically focusable only (never a Tab stop):
  // the #scene3d container and the callout buttons own Tab order.
  try { dom.tabIndex = -1; } catch (e) { /* non-DOM renderer in tests */ }
  injectCalloutFocusStyle();
  // Callouts live in the label layer inside the canvas's parent
  // (#scene3d). Delegating here keeps one listener for callouts that
  // are rebuilt on every Report; if the canvas has no parent yet,
  // fall back to the document.
  const keyHost = dom.parentElement || (typeof document !== 'undefined' ? document : null);

  // --- mesh registry -------------------------------------------------------
  let meshMap = new Map(); // componentId -> Object3D (mesh or group)
  let objectToId = new Map(); // any Object3D in a component subtree -> componentId

  function rebuildRegistry() {
    objectToId = new Map();
    meshMap.forEach((root, id) => {
      if (!root) return;
      root.traverse((obj) => objectToId.set(obj, id));
    });
  }

  function setMeshes(next) {
    meshMap = new Map();
    if (next instanceof Map) {
      next.forEach((root, id) => meshMap.set(String(id), root));
    } else if (next && typeof next === 'object') {
      Object.keys(next).forEach((id) => meshMap.set(String(id), next[id]));
    }
    rebuildRegistry();
  }
  setMeshes(meshesById);

  function componentIdOf(object) {
    if (!object) return null;
    let obj = object;
    while (obj) {
      if (objectToId.has(obj)) return objectToId.get(obj);
      const ud = obj.userData || {};
      const raw = ud.componentId != null ? ud.componentId : ud.id;
      if (raw != null && meshMap.has(String(raw))) return String(raw);
      obj = obj.parent;
    }
    return null;
  }

  function componentData(id, root) {
    const ud = (root && root.userData) || {};
    const data = { id };
    if (ud.type != null) data.type = ud.type;
    if (ud.value != null) data.value = ud.value;
    if (ud.nodes != null) data.nodes = ud.nodes;
    if (ud.node_voltage != null) data.node_voltage = ud.node_voltage;
    if (ud.current != null) data.current = ud.current;
    if (ud.power != null) data.power = ud.power;
    return data;
  }

  // --- tooltip -------------------------------------------------------------
  const tooltip = document.createElement('div');
  tooltip.setAttribute('role', 'tooltip');
  tooltip.style.cssText = [
    'position:absolute', 'display:none', 'pointer-events:none', 'z-index:10',
    'max-width:260px', 'padding:6px 9px', 'border-radius:6px',
    'background:rgba(17,17,17,0.92)', 'color:#fff',
    'font:12px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif',
    'white-space:pre-line', 'box-shadow:0 2px 8px rgba(0,0,0,0.35)',
  ].join(';');
  const tooltipParent = dom.parentElement || document.body;
  if (tooltipParent !== document.body) {
    const pos = window.getComputedStyle(tooltipParent).position;
    if (pos === 'static') tooltipParent.style.position = 'relative';
  } else {
    tooltip.style.position = 'fixed';
  }
  tooltipParent.appendChild(tooltip);

  function showTooltip(data, clientX, clientY) {
    const lines = [data.id];
    if (data.type != null) lines.push('type: ' + escapeText(data.type));
    if (data.value != null) lines.push('value: ' + escapeText(data.value));
    if (data.nodes != null) {
      const nodes = Array.isArray(data.nodes) ? data.nodes.join(' ') : escapeText(data.nodes);
      lines.push('nodes: ' + nodes);
    }
    // Simulation-filled fields are shown ONLY if the main page / server
    // already supplied them on the mesh; this module never computes them.
    if (data.node_voltage != null) lines.push('node voltage: ' + escapeText(data.node_voltage));
    if (data.current != null) lines.push('current: ' + escapeText(data.current));
    if (data.power != null) lines.push('power: ' + escapeText(data.power));
    tooltip.textContent = lines.join('\n');
    tooltip.style.display = 'block';
    const rect = tooltipParent.getBoundingClientRect();
    const x = clientX - rect.left + 14;
    const y = clientY - rect.top + 14;
    tooltip.style.left = Math.min(x, Math.max(0, rect.width - 140)) + 'px';
    tooltip.style.top = Math.min(y, Math.max(0, rect.height - 60)) + 'px';
  }

  function hideTooltip() {
    tooltip.style.display = 'none';
  }

  // --- selection / highlight ----------------------------------------------
  let selectedId = null;
  let selectedRoot = null;
  let outline = null; // THREE.BoxHelper drawn around the selected component
  // componentId -> [{ mesh, material, emissiveHex, emissiveIntensity }]
  const savedMaterials = new Map();
  let controls = null; // optional OrbitControls-like object, set via setControls()
  let announceTimer = null;

  function materialUsageCounts() {
    const counts = new Map();
    meshMap.forEach((root) => {
      if (!root) return;
      root.traverse((obj) => {
        if (!obj.isMesh) return;
        const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
        mats.forEach((m) => { if (m) counts.set(m, (counts.get(m) || 0) + 1); });
      });
    });
    return counts;
  }

  function applyHighlight(root, id) {
    clearHighlight();
    if (!root) return;
    const counts = materialUsageCounts();
    const saved = [];
    root.traverse((obj) => {
      if (!obj.isMesh) return;
      const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
      mats.forEach((mat, i) => {
        if (!mat || !('emissive' in mat)) return;
        // A shared material must be cloned first, otherwise highlighting
        // this component would light up every component using it.
        let target = mat;
        if ((counts.get(mat) || 0) > 1) {
          target = mat.clone();
          if (Array.isArray(obj.material)) obj.material[i] = target;
          else obj.material = target;
        }
        saved.push({
          mesh: obj, index: i, material: mat, target,
          emissiveHex: target.emissive.getHex(),
          emissiveIntensity: target.emissiveIntensity,
        });
        target.emissive.setHex(HIGHLIGHT_EMISSIVE);
        target.emissiveIntensity = HIGHLIGHT_EMISSIVE_INTENSITY;
      });
    });
    savedMaterials.set(id, saved);
    outline = new THREE.BoxHelper(root, OUTLINE_COLOR);
    outline.name = 'netproof-selection-outline';
    scene.add(outline);
  }

  function clearHighlight() {
    if (outline) {
      scene.remove(outline);
      if (outline.geometry) outline.geometry.dispose();
      if (outline.material) outline.material.dispose();
      outline = null;
    }
    savedMaterials.forEach((saved) => {
      saved.forEach((entry) => {
        entry.target.emissive.setHex(entry.emissiveHex);
        entry.target.emissiveIntensity = entry.emissiveIntensity;
        // Restore the original (shared) material on the mesh.
        if (entry.target !== entry.material) {
          if (Array.isArray(entry.mesh.material)) entry.mesh.material[entry.index] = entry.material;
          else entry.mesh.material = entry.material;
          entry.target.dispose();
        }
      });
    });
    savedMaterials.clear();
  }

  // Announce the settled selection to #selected-announcer (a11y spec
  // §9.1), debounced so holding an arrow key does not spam the screen
  // reader. Silent no-op when the live region does not exist yet.
  // Text comes only from the mesh's own userData (id / type / value);
  // no verdict or measurement is derived here.
  function announceSelection() {
    if (typeof document === 'undefined') return;
    const region = document.getElementById('selected-announcer');
    if (!region) return;
    if (announceTimer) clearTimeout(announceTimer);
    announceTimer = setTimeout(() => {
      announceTimer = null;
      if (!selectedId) {
        region.textContent = 'No component selected.';
        return;
      }
      const data = componentData(selectedId, meshMap.get(selectedId));
      const bits = [selectedId + ' selected.'];
      if (data.type != null) {
        bits.push(String(data.type) + (data.value != null ? ', ' + escapeText(data.value) : '') + '.');
      }
      region.textContent = bits.join(' ');
    }, ANNOUNCE_DEBOUNCE_MS);
  }

  function select(id, emit = true) {
    if (id != null && !meshMap.has(String(id))) id = null;
    if (id === selectedId) return selectedId;
    clearHighlight();
    selectedId = id == null ? null : String(id);
    selectedRoot = selectedId ? meshMap.get(selectedId) : null;
    if (selectedRoot) applyHighlight(selectedRoot, selectedId);
    if (emit && typeof cb.onSelect === 'function') cb.onSelect(selectedId);
    announceSelection();
    return selectedId;
  }

  function deselect(emit = true) {
    return select(null, emit);
  }

  // --- callouts (keyboard) -------------------------------------------------
  // Callouts are real <button class="netproof-callout"> elements owned
  // by the overlay/callouts layer, one per component, in netlist (DOM)
  // order. They are rebuilt on every Report, so they are always queried
  // fresh — never cached. Hidden callouts (display:none until projected)
  // are not keyboard reachable and are filtered out.

  function calloutElements() {
    const scope = keyHost || (typeof document !== 'undefined' ? document : null);
    if (!scope || typeof scope.querySelectorAll !== 'function') return [];
    return Array.from(scope.querySelectorAll('.netproof-callout'))
      .filter((el) => !el.disabled && el.offsetParent !== null);
  }

  function componentIdsInOrder() {
    return Array.from(meshMap.keys());
  }

  // The component id a callout stands for: its data-component-id, or
  // — if the layer did not stamp one — the netlist id at the same index
  // in DOM order (callout order is netlist order by contract).
  function idForCallout(callout, index) {
    const stamped = callout && callout.getAttribute
      ? callout.getAttribute('data-component-id') : null;
    if (stamped) return stamped;
    const ids = componentIdsInOrder();
    return (index >= 0 && index < ids.length) ? ids[index] : null;
  }

  function focusCallout(id) {
    if (id == null) return false;
    const wanted = String(id);
    const list = calloutElements();
    for (let i = 0; i < list.length; i++) {
      if (idForCallout(list[i], i) === wanted) {
        list[i].focus();
        return true;
      }
    }
    return false;
  }

  // Next / previous component id with wrap. From no selection, an
  // arrow forward lands on the first part, backward on the last.
  function stepId(fromId, dir) {
    const ids = componentIdsInOrder();
    if (!ids.length) return null;
    let idx = fromId == null ? -1 : ids.indexOf(String(fromId));
    if (idx === -1) return dir >= 0 ? ids[0] : ids[ids.length - 1];
    return ids[(idx + dir + ids.length) % ids.length];
  }

  function cycleSelection(dir) {
    const next = stepId(selectedId, dir >= 0 ? 1 : -1);
    if (next == null) return null;
    return select(next);
  }

  function openEdit(id) {
    if (id == null) return;
    if (String(id) !== selectedId) select(id);
    if (typeof cb.onEdit === 'function') cb.onEdit(String(id));
  }

  function focusSceneContainer() {
    const host = dom.parentElement;
    if (host && host.tabIndex >= 0 && typeof host.focus === 'function') {
      host.focus();
      return;
    }
    // The container is not focusable (or has no parent): the canvas
    // itself is programmatically focusable, so focus never gets lost.
    dom.focus();
  }

  // Canvas keys: only when the canvas itself is the target. The
  // #scene3d container handler in index.html only fires when the
  // container is the target, so this never double-steps with it.
  // "R" is deliberately not handled here: main3d.js owns Reset view.
  function onCanvasKeyDown(event) {
    if (!event || event.target !== dom) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const key = event.key;
    if (key === 'ArrowRight' || key === 'ArrowDown') {
      event.preventDefault();
      cycleSelection(1);
    } else if (key === 'ArrowLeft' || key === 'ArrowUp') {
      event.preventDefault();
      cycleSelection(-1);
    } else if (key === 'Home') {
      event.preventDefault();
      const ids = componentIdsInOrder();
      if (ids.length) select(ids[0]);
    } else if (key === 'End') {
      event.preventDefault();
      const ids = componentIdsInOrder();
      if (ids.length) select(ids[ids.length - 1]);
    } else if (key === 'Enter') {
      event.preventDefault();
      if (selectedId) openEdit(selectedId);
      else cycleSelection(1);
    } else if (key === 'Escape') {
      event.preventDefault();
      deselect();
    }
    // Space and every other key: ignored, never hijacked.
  }

  // Callout keys, delegated on the #scene3d container (or the document
  // when the canvas has no parent). Fires only when the event target
  // is a callout button, so it never fights the container handler.
  function onSceneKeyDown(event) {
    if (!event || !event.target || typeof event.target.closest !== 'function') return;
    const callout = event.target.closest('.netproof-callout');
    if (!callout) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const key = event.key;
    const list = calloutElements();
    const index = list.indexOf(callout);
    if (key === 'ArrowRight' || key === 'ArrowDown') {
      event.preventDefault();
      if (!list.length) return;
      const next = list[(index + 1) % list.length];
      const id = idForCallout(next, (index + 1) % list.length);
      if (id) select(id);
      next.focus();
    } else if (key === 'ArrowLeft' || key === 'ArrowUp') {
      event.preventDefault();
      if (!list.length) return;
      const prev = list[(index - 1 + list.length) % list.length];
      const id = idForCallout(prev, (index - 1 + list.length) % list.length);
      if (id) select(id);
      prev.focus();
    } else if (key === 'Home') {
      event.preventDefault();
      if (!list.length) return;
      const id = idForCallout(list[0], 0);
      if (id) select(id);
      list[0].focus();
    } else if (key === 'End') {
      event.preventDefault();
      if (!list.length) return;
      const last = list[list.length - 1];
      const id = idForCallout(last, list.length - 1);
      if (id) select(id);
      last.focus();
    } else if (key === 'Enter' || key === ' ') {
      event.preventDefault();
      const id = idForCallout(callout, index);
      openEdit(id != null ? id : selectedId);
    } else if (key === 'Escape') {
      event.preventDefault();
      deselect();
      focusSceneContainer();
    }
    // Tab / Shift+Tab are native (one stop per callout, DOM order);
    // every other key is ignored.
  }

  // Window Escape: deselects, unless the edit panel owns this Escape
  // (focus inside #netproof-edit-panel — the panel closes itself and
  // returns focus), the key was already handled (defaultPrevented),
  // or focus is in a typing control (inputs / selects own Escape).
  function onKeyDown(event) {
    if (!event || event.key !== 'Escape' || !selectedId) return;
    if (event.defaultPrevented) return;
    const t = event.target;
    if (t && typeof t.closest === 'function' && t.closest('#netproof-edit-panel')) return;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA')) return;
    deselect();
  }

  // --- raycasting ----------------------------------------------------------
  function setPointerFromEvent(event) {
    const rect = dom.getBoundingClientRect();
    pointerNdc.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointerNdc.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointerNdc, camera);
  }

  function pick(event) {
    setPointerFromEvent(event);
    const roots = Array.from(meshMap.values()).filter(Boolean);
    if (!roots.length) return null;
    const hits = raycaster.intersectObjects(roots, true);
    if (!hits.length) return null;
    return { id: componentIdOf(hits[0].object), object: hits[0].object, point: hits[0].point };
  }

  // --- wire / node update --------------------------------------------------
  function shiftPositions(container, delta) {
    if (!container) return;
    const shift = (v) => {
      if (isPlainPosition(v) || (v && typeof v.add === 'function')) v.add(delta);
    };
    if (Array.isArray(container)) container.forEach(shift);
    else if (typeof container === 'object') Object.keys(container).forEach((k) => shift(container[k]));
  }

  function refreshWires(componentId, newPosition, deltaArg) {
    const id = String(componentId);
    const root = meshMap.get(id);
    const newPos = newPosition
      ? new THREE.Vector3(newPosition.x, newPosition.y, newPosition.z)
      : (root ? root.position.clone() : null);
    if (!newPos) return;
    // Callers that already moved the mesh pass the delta explicitly (drag
    // path). Otherwise derive it from the requested position vs. current.
    const delta = deltaArg
      ? new THREE.Vector3(deltaArg.x, deltaArg.y, deltaArg.z)
      : (newPosition && root ? newPos.clone().sub(root.position) : new THREE.Vector3(0, 0, 0));
    // Node positions stored on the component itself shift with it.
    if (root && root.userData.nodePositions) shiftPositions(root.userData.nodePositions, delta);

    scene.traverse((obj) => {
      const ud = obj.userData || {};
      // Node anchors belonging to this component move with it.
      if (ud.nodeAnchorFor === id) {
        if (isPlainPosition(ud.anchorOffset)) {
          obj.position.set(newPos.x + ud.anchorOffset.x, newPos.y + ud.anchorOffset.y, newPos.z + ud.anchorOffset.z);
        } else {
          obj.position.add(delta);
        }
      }
      // Wires attached to this component.
      const isWire = ud.wire === true || ud.isWire === true;
      if (!isWire) return;
      const attached = ud.wireFor === id || ud.from === id || ud.to === id ||
        (Array.isArray(ud.components) && ud.components.includes(id)) ||
        (Array.isArray(ud.endpoints) && ud.endpoints.some((e) => e && e.componentId === id));
      if (!attached) return;
      if (typeof ud.update === 'function') {
        ud.update(id, toPlainPosition(newPos), toPlainPosition(delta));
        return;
      }
      if (typeof ud.refreshWire === 'function') {
        ud.refreshWire(id, toPlainPosition(newPos), toPlainPosition(delta));
        return;
      }
      // Generic line wire: endpoints name the component + a local offset.
      if (obj.isLine && Array.isArray(ud.endpoints) && obj.geometry) {
        const attr = obj.geometry.getAttribute('position');
        if (attr) {
          ud.endpoints.forEach((endpoint, i) => {
            if (!endpoint || endpoint.componentId == null || i >= attr.count) return;
            const compRoot = meshMap.get(String(endpoint.componentId));
            if (!compRoot) return;
            const off = endpoint.offset || { x: 0, y: 0, z: 0 };
            attr.setXYZ(i,
              compRoot.position.x + (off.x || 0),
              compRoot.position.y + (off.y || 0),
              compRoot.position.z + (off.z || 0));
          });
          attr.needsUpdate = true;
          obj.geometry.computeBoundingSphere();
        }
      }
      // TubeGeometry wires without an update hook cannot be rebuilt here;
      // their owner must register userData.update / refreshWire (see header).
    });

    if (outline) outline.update();
  }

  // --- drag state ----------------------------------------------------------
  let press = null; // { x, y, id, root, point, offset, moved }
  let dragging = false;
  let lastEmittedPosition = null;
  let suppressDblClickUntil = 0;
  let controlsWereEnabled = true;

  function emitMove(id, plainPos) {
    if (typeof cb.onMove !== 'function') return;
    if (lastEmittedPosition && positionsEqual(lastEmittedPosition, plainPos)) return;
    lastEmittedPosition = { ...plainPos };
    cb.onMove(id, { ...plainPos });
  }

  function onPointerDown(event) {
    if (event.button !== undefined && event.button !== 0) return;
    const hit = pick(event);
    const id = hit ? hit.id : null;
    let offset = new THREE.Vector3(0, 0, 0);
    if (hit && hit.point) {
      const root = meshMap.get(id);
      // Measure the grab offset on the same ground plane the drag moves on
      // (not the mesh surface hit): for a tall part the surface point sits
      // above the plane, and mixing the two made the part jump on drag start.
      const ground = new THREE.Vector3();
      if (root) {
        setPointerFromEvent(event);
        offset.copy(root.position).sub(raycaster.ray.intersectPlane(groundPlane, ground) ? ground : hit.point);
      }
    }
    press = { x: event.clientX, y: event.clientY, id, point: hit ? hit.point : null, offset, moved: false };
    if (hit) {
      try { dom.setPointerCapture(event.pointerId); } catch (e) { /* not supported */ }
    }
    hideTooltip();
  }

  function onPointerMove(event) {
    if (!press) {
      // Hover: tooltip only, no selection change.
      if (dragging) return;
      const hit = pick(event);
      if (hit && hit.id) {
        const root = meshMap.get(hit.id);
        showTooltip(componentData(hit.id, root), event.clientX, event.clientY);
        dom.style.cursor = 'pointer';
      } else {
        hideTooltip();
        dom.style.cursor = '';
      }
      return;
    }
    const dx = event.clientX - press.x;
    const dy = event.clientY - press.y;
    if (!dragging && Math.hypot(dx, dy) < CLICK_THRESHOLD_PX) return;
    if (!press.id) return; // pressed empty space: never a drag

    if (!dragging) {
      dragging = true;
      press.moved = true;
      if (press.id !== selectedId) select(press.id);
      if (controls) {
        controlsWereEnabled = controls.enabled !== false;
        controls.enabled = false;
      }
      if (typeof cb.onDragStart === 'function') cb.onDragStart(press.id);
      hideTooltip();
      dom.style.cursor = 'grabbing';
    }

    // Drag on the ground plane (y = 0); keep the component's own height.
    setPointerFromEvent(event);
    const hitPoint = new THREE.Vector3();
    if (!raycaster.ray.intersectPlane(groundPlane, hitPoint)) return;
    const root = meshMap.get(press.id);
    if (!root) return;
    const oldPos = root.position.clone();
    const next = new THREE.Vector3(
      hitPoint.x + press.offset.x,
      root.position.y,
      hitPoint.z + press.offset.z,
    );
    const delta = next.clone().sub(oldPos);
    if (delta.lengthSq() < EPSILON * EPSILON) return;
    root.position.copy(next);
    refreshWires(press.id, toPlainPosition(next), toPlainPosition(delta));
    emitMove(press.id, toPlainPosition(next));
  }

  function onPointerUp(event) {
    if (!press) return;
    const wasDragging = dragging;
    const pressId = press.id;
    const hadMoved = press.moved;
    press = null;
    if (wasDragging) {
      dragging = false;
      dom.style.cursor = '';
      if (controls) controls.enabled = controlsWereEnabled;
      // Final onMove if the last live position was not already emitted.
      const root = pressId ? meshMap.get(pressId) : null;
      if (root) emitMove(pressId, toPlainPosition(root.position));
      if (typeof cb.onDragEnd === 'function' && pressId) {
        cb.onDragEnd(pressId, root ? toPlainPosition(root.position) : null);
      }
      suppressDblClickUntil = Date.now() + 350;
      return;
    }
    // Plain click (no drag): select the hit component, or deselect.
    if (!hadMoved) {
      if (pressId) select(pressId);
      else deselect();
    }
  }

  function onDoubleClick(event) {
    if (Date.now() < suppressDblClickUntil) return;
    const hit = pick(event);
    if (hit && hit.id) {
      openEdit(hit.id);
    }
  }

  function onPointerLeave() {
    hideTooltip();
    if (!dragging) dom.style.cursor = '';
  }

  dom.addEventListener('pointerdown', onPointerDown);
  dom.addEventListener('pointermove', onPointerMove);
  dom.addEventListener('pointerup', onPointerUp);
  dom.addEventListener('pointercancel', onPointerUp);
  dom.addEventListener('dblclick', onDoubleClick);
  dom.addEventListener('pointerleave', onPointerLeave);
  dom.addEventListener('keydown', onCanvasKeyDown);
  if (keyHost) keyHost.addEventListener('keydown', onSceneKeyDown);
  window.addEventListener('keydown', onKeyDown);
  dom.style.touchAction = 'none';

  function dispose() {
    dom.removeEventListener('pointerdown', onPointerDown);
    dom.removeEventListener('pointermove', onPointerMove);
    dom.removeEventListener('pointerup', onPointerUp);
    dom.removeEventListener('pointercancel', onPointerUp);
    dom.removeEventListener('dblclick', onDoubleClick);
    dom.removeEventListener('pointerleave', onPointerLeave);
    dom.removeEventListener('keydown', onCanvasKeyDown);
    if (keyHost) keyHost.removeEventListener('keydown', onSceneKeyDown);
    window.removeEventListener('keydown', onKeyDown);
    if (announceTimer) { clearTimeout(announceTimer); announceTimer = null; }
    clearHighlight();
    hideTooltip();
    if (tooltip.parentElement) tooltip.parentElement.removeChild(tooltip);
    selectedId = null;
    selectedRoot = null;
    press = null;
    dragging = false;
  }

  return {
    dispose,
    select,
    deselect,
    getSelectedId() { return selectedId; },
    setMeshes,
    setControls(next) { controls = next || null; },
    refreshWires,
    focusCallout,
    cycleSelection,
  };
}

// Non-module fallback: index.html is plain script today, so expose the same
// entry point globally when loaded without a module loader. Module users
// should use the named export above.
if (typeof window !== 'undefined') {
  window.NetproofInteraction = window.NetproofInteraction || {};
  window.NetproofInteraction.setupInteraction = setupInteraction;
}
