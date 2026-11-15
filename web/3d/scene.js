// Netproof 3D scene — BLUEPRINT render (wholesale rebuild).
//
// Orthographic isometric camera, white / pale-blue MeshBasicMaterial
// fills with blue EdgesGeometry outlines, no lights, no fog, no shadows,
// no dark ground. Component geometry lives in ./components.js (shared
// builders); this file owns the camera, the view, framing, verdict
// edge-recolouring + glow, and the Inspect explode tween.
//
// Usage (from a page that loads this as an ES module):
//   import { createScene, addComponent, removeComponent, updateVerdictColors }
//     from './3d/scene.js';
//   const view = createScene(document.getElementById('scene-container'), {
//     onPick: (componentId) => console.log('picked', componentId),
//   });
//   addComponent(view, { id: 'R1', type: 'resistor', value: 330, nodes: ['n1', '0'] });
//   updateVerdictColors(view, { R1: 'PASS' });
//
// Component shape mirrors the backend IR (backend/netproof/ir.py):
//   { id, type, value, nodes: [n1, n2], rating_watts?, color? }
//   type is one of: resistor | capacitor | led | diode | voltage_source
//                  | current_source. Unknown types get a generic box.
// Verdicts mirror backend/netproof/ir.py Verdict: PASS | WARN | STOP.
// No simulation numbers live here: colours come only from the verdict
// values the caller passes in (which must come from a real /check run).
//
// Blueprint blue is #0B3D91 ONLY (coordinator decision 2026-11-15).
// Severity edge colours are STOP #d92d20, WARN #92400E, PASS #1a7f37 —
// the same values overlay.js SEVERITY_COLORS and history.js
// VERDICT_COLORS use (coordinator decision 2026-11-15: WARN #d97706
// failed AA as text). The three verdict-colour tables
// (scene.js, overlay.js, history.js) must change together; changing one
// without the others is a known trap (see web/3d/README-3D.md).
//
// NOTE on imports: every module in web/3d/ imports the bare specifier
// 'three' (and 'three/addons/...' for OrbitControls), resolved by the
// host page's import map to unpkg three@0.160.0, so the whole 3D layer
// shares exactly one THREE instance. OrbitControls imports bare
// 'three' internally too, so the map must be present before this
// module can run in a browser. That map belongs in web/index.html,
// which this file must not edit. Do NOT import three from a direct
// CDN URL: a second URL would load a second THREE instance.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import {
  BLUEPRINT_BLUE,
  BLUEPRINT_FILL,
  BLUEPRINT_FILL_2,
  addEdges,
  blueprintMaterial,
  buildComponentMesh,
  easeInOutCubic,
  explodeOffset,
  EXPLODE_DURATION_MS,
} from './components.js';

export { addEdges, blueprintMaterial };

// Blueprint palette (single source of truth for this file; the values
// match components.js — one blue, #0B3D91, everywhere).
export const BLUEPRINT = Object.freeze({
  background: 0x34373e, // matches the lit floor colour
  fill: BLUEPRINT_FILL,
  fill2: BLUEPRINT_FILL_2,
  edge: BLUEPRINT_BLUE,
  ink: 0x1e3a5f,
});

// Verdict -> EDGE colour. Severity changes edges + glow only; fills stay
// white / pale-blue and wires stay blue. Matches overlay.js
// SEVERITY_COLORS exactly (STOP #d92d20, WARN #92400E, PASS #1a7f37).
// UNKNOWN is blueprint blue with no glow: no report, no signal.
export const VERDICT_COLORS = Object.freeze({
  PASS: 0x1a7f37,
  WARN: 0x92400e,
  STOP: 0xd92d20,
  INFO: 0x667085, // never colours a part; INFO resolves to neutral blue
  UNKNOWN: BLUEPRINT_BLUE,
});

// Default body fill per component type. Type is read from shape + label,
// not colour, so these are all white / pale-blue by design.
export const COMPONENT_COLORS = Object.freeze({
  resistor: BLUEPRINT_FILL,
  capacitor: BLUEPRINT_FILL_2,
  led: BLUEPRINT_FILL_2,
  diode: BLUEPRINT_FILL,
  voltage_source: BLUEPRINT_FILL_2,
  current_source: BLUEPRINT_FILL,
  default: BLUEPRINT_FILL,
});

const COMPONENT_TYPES = new Set([
  'resistor',
  'capacitor',
  'led',
  'diode',
  'voltage_source',
  'current_source',
]);

const SPACING = 2.2; // layout spacing between components on the x axis (scene units, not electrical)
const GROUND_Y = 0;

// Classic isometric view: elevation 35.264 deg, azimuth 45 deg, giving
// dir = (cos(el)*sin(az), sin(el), cos(el)*cos(az)) = (0.577, 0.577, 0.577)
// (normalised). All three axes are equally foreshortened: true isometric.
export const ISO_ELEVATION_DEG = 35.264;
export const ISO_AZIMUTH_DEG = 45;
const _el = THREE.MathUtils.degToRad(ISO_ELEVATION_DEG);
const _az = THREE.MathUtils.degToRad(ISO_AZIMUTH_DEG);
export const ISO_DIRECTION = Object.freeze(new THREE.Vector3(
  Math.cos(_el) * Math.sin(_az),
  Math.sin(_el),
  Math.cos(_el) * Math.cos(_az),
).normalize());

const UP = new THREE.Vector3(0, 1, 0);
const CAMERA_DISTANCE = 50; // arbitrary in ortho (only near/far care); the frustum does the fitting
const ORTHO_NEAR = -100;
const ORTHO_FAR = 100;

function normalizeType(type) {
  const t = String(type || '').trim().toLowerCase();
  return COMPONENT_TYPES.has(t) ? t : 'default';
}

function normalizeVerdict(verdict) {
  const v = String(verdict || '').trim().toUpperCase();
  if (v === 'PASS' || v === 'WARN' || v === 'STOP' || v === 'INFO') return v;
  if (v === 'WARNING') return 'WARN';
  if (v === 'FAIL' || v === 'ERROR') return 'STOP';
  return 'UNKNOWN';
}

function verdictColor(verdict) {
  return VERDICT_COLORS[normalizeVerdict(verdict)];
}

function prefersReducedMotion() {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

// Mesh-only bounding box of a group: glow sprites are excluded (their
// geometry carries an empty bounding box by construction — see
// components.js finalizeGroup — but this stays correct even for groups
// built any other way). Edges (LineSegments) share their mesh's bounds.
function meshBox(root) {
  const box = new THREE.Box3();
  root.updateWorldMatrix(true, true);
  root.traverse((obj) => {
    if (!obj.isMesh) return;
    const b = new THREE.Box3().setFromObject(obj);
    if (!b.isEmpty()) box.union(b);
  });
  return box;
}

// Severity: edge recolour + soft glow, rest stays blue.
// A finding changes only the affected part's edges and its glow sprite.
// Fill stays white / pale-blue, wires stay blue, every other part stays
// exactly as it was. UNKNOWN (and INFO, which never colours) restores
// blue edges and hides the glow. PASS glows fainter (0.18) than
// WARN / STOP (0.35). No PASS is ever inferred here: callers pass the
// verdict, and updateVerdictColors / overlay severityForComponent own
// the worst-severity-wins rule.
function applyVerdictToGroup(group, verdict) {
  const v = normalizeVerdict(verdict);
  group.userData.verdict = v;
  const neutral = v === 'UNKNOWN' || v === 'INFO' || v === 'PASS';
  // Neutral parts keep a faint dark outline; WARN / STOP light it up.
  const edgeHex = neutral ? 0x000000 : VERDICT_COLORS[v];
  const edgeOpacity = neutral ? 0.14 : 1;
  const edgeMat = group.userData.edgeMaterial;
  if (edgeMat && edgeMat.color) {
    edgeMat.color.setHex(edgeHex);
    edgeMat.transparent = true;
    edgeMat.opacity = edgeOpacity;
  }
  // Groups built without a shared edge material (or with extra
  // per-mesh edge materials) still recolour through the stored material.
  group.traverse((child) => {
    if (!child.isMesh) return;
    const m = child.userData.edgeMaterial;
    if (m && m !== edgeMat && m.color) {
      m.color.setHex(edgeHex);
      m.transparent = true;
      m.opacity = edgeOpacity;
    }
  });
  const glow = group.userData.glowSprite;
  if (glow) {
    if (neutral) {
      glow.visible = false;
    } else {
      glow.material.color.setHex(VERDICT_COLORS[v]);
      glow.material.opacity = v === 'PASS' ? 0.18 : 0.35;
      glow.visible = true;
    }
  }
}

function layoutPosition(index, position) {
  if (position && Number.isFinite(position.x) && Number.isFinite(position.z)) {
    return { x: position.x, z: position.z };
  }
  return { x: (index - 0) * SPACING, z: 0 };
}

/**
 * Frame the camera on the circuit's bounding box (orthographic fit).
 *
 * Computes the Box3 union of every mesh in `view.components`, expands
 * it for the slab and dimension lines (slab pad 0.8 + dimension offset
 * 0.5 = 1.3 per side in X/Z, slab bottom 0.18 in Y), then sizes the
 * ortho frustum from the exact corner fit: project the 8 box corners
 * onto the camera's right/up axes, take max |x| / max |y|, apply
 * `options.margin` (default 1.15), enforce the aspect, and set
 * left/right/top/bottom with zoom reset to 1. The current view
 * direction is kept (so an orbit angle the user chose survives); only
 * the frustum, target and position change. Returns false (and moves
 * nothing) when there are no components, so an empty scene keeps its
 * view. Callers: main3d.js buildDisplayed() — i.e. on load and on every
 * setNetlist / history-round rebuild — via resetToIso (full reset) or
 * directly. Also exposed as view.frameCamera.
 */
export function frameCamera(view, options = {}) {
  if (!view || !view.camera || !view.controls
    || !view.components || !view.components.size) return false;
  const box = new THREE.Box3();
  let hasBox = false;
  view.components.forEach((group) => {
    if (!group) return;
    const b = meshBox(group);
    if (b.isEmpty()) return;
    box.union(b);
    hasBox = true;
  });
  if (!hasBox || box.isEmpty()) return false;

  // Include the slab and dimension lines in the fit, so the fit never
  // crops the slab edge or a dimension line.
  box.min.x -= 1.3;
  box.max.x += 1.3;
  box.min.z -= 1.3;
  box.max.z += 1.3;
  box.min.y -= SLAB_BOTTOM_PAD;

  const center = box.getCenter(new THREE.Vector3());
  const margin = Number.isFinite(options.margin) && options.margin > 0
    ? options.margin
    : 1.15;

  // Keep the current viewing direction; only frustum/target change.
  const direction = view.camera.position.clone().sub(view.controls.target);
  if (direction.lengthSq() < 1e-6) direction.copy(ISO_DIRECTION);
  direction.normalize();

  const right = new THREE.Vector3().crossVectors(direction, UP);
  if (right.lengthSq() < 1e-6) right.set(1, 0, 0); // looking straight down
  right.normalize();
  const up = new THREE.Vector3().crossVectors(right, direction).normalize();

  // Exact corner fit: a bounding sphere would force the long, flat LED
  // circuit to fit its 6-unit length into the viewport height; the
  // corner fit fills the width instead.
  let halfW = 0;
  let halfH = 0;
  for (const x of [box.min.x, box.max.x]) {
    for (const y of [box.min.y, box.max.y]) {
      for (const z of [box.min.z, box.max.z]) {
        const v = new THREE.Vector3(x, y, z).sub(center);
        halfW = Math.max(halfW, Math.abs(v.dot(right)));
        halfH = Math.max(halfH, Math.abs(v.dot(up)));
      }
    }
  }
  halfW *= margin;
  halfH *= margin;
  const aspect = Math.max(view.camera.aspect || 1, 0.01);
  halfH = Math.max(halfH, halfW / aspect);
  halfW = halfH * aspect;

  const cam = view.camera;
  cam.left = -halfW;
  cam.right = halfW;
  cam.top = halfH;
  cam.bottom = -halfH;
  cam.zoom = 1;
  cam.updateProjectionMatrix();
  view._orthoHalfH = halfH;

  view.controls.target.copy(center);
  cam.position.copy(center).addScaledVector(direction, CAMERA_DISTANCE);
  view.controls.update();
  return true;
}

// Slab bottom sits 0.18 below y = 0 (slab height); the fit includes it.
const SLAB_BOTTOM_PAD = 0.18;

/**
 * Reset to the canonical isometric view: discard any user orbit, set
 * the camera direction back to (0.577, 0.577, 0.577), then fit the
 * bounding box with frameCamera. That is the whole reset: direction +
 * fit, nothing else. Call it from buildDisplayed() on load and on every
 * setNetlist / history-round rebuild (a new netlist is a new drawing).
 * A plain updateReport (no netlist change) must NOT call it: it only
 * re-tints and re-labels. Also exposed as view.resetToIso().
 */
export function resetToIso(view) {
  if (!view || !view.camera || !view.controls) return false;
  const center = view.controls.target.clone();
  view.camera.position.copy(center).addScaledVector(ISO_DIRECTION, CAMERA_DISTANCE);
  view.controls.update();
  return frameCamera(view);
}

/**
 * Create the Three.js scene inside `container`.
 *
 * Orthographic isometric camera (35.264 deg elevation, 45 deg azimuth),
 * white background, OrbitControls (rotate / pan / wheel zoom via
 * camera.zoom 0.5-8, polar clamped so the camera never goes under the
 * slab). No lights, no fog, no shadows, no ground grid: MeshBasicMaterial
 * is unlit, and the blueprint has one raised slab, not an infinite grid.
 *
 * @param {HTMLElement} container - element the renderer canvas is appended to.
 * @param {object} [options]
 * @param {(componentId: string | null) => void} [options.onPick] - called
 *   with the picked component id, or null when empty space is clicked.
 * @param {number} [options.background] - scene background colour (default white).
 * @returns {{ scene, camera, renderer, controls, components: Map,
 *   raycaster, selectedId, pickAt, setOnPick, frameCamera, resetToIso,
 *   setExploded, toggleExplode, isExploded, stepExplode, resetExplode,
 *   dispose }}
 *   `components` maps component id -> mesh group. The render loop runs
 *   until `dispose()` is called. `frameCamera()` fits the camera to
 *   the bounding box of every component (see frameCamera);
 *   `resetToIso()` restores the canonical iso direction and fits.
 *   Explode state: `setExploded(bool)` tweens every component group to
 *   its staggered offset (0.9 + 0.18 * index) over 300 ms with
 *   easeInOutCubic, driven by the animate loop (and by `stepExplode(now)`
 *   for callers with their own tick); `resetExplode()` snaps every group
 *   back to its base Y and clears the flag — call it on setNetlist.
 */
export function createScene(container, options = {}) {
  if (!container || typeof container.appendChild !== 'function') {
    throw new TypeError('createScene requires a DOM container element');
  }
  const onPick = typeof options.onPick === 'function' ? options.onPick : null;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(options.background ?? BLUEPRINT.background);
  // Lit bench: soft sky/ground fill + one shadow-casting key light.
  scene.add(new THREE.HemisphereLight(0xffffff, 0x3a3f48, 1.25));
  const key = new THREE.DirectionalLight(0xffffff, 1.7);
  key.position.set(6, 14, 8);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.left = -14;
  key.shadow.camera.right = 14;
  key.shadow.camera.top = 14;
  key.shadow.camera.bottom = -14;
  key.shadow.camera.near = 0.5;
  key.shadow.camera.far = 60;
  key.shadow.bias = -0.0005;
  key.shadow.radius = 4;
  scene.add(key);
  // Bench floor under the mat: catches the mat's shadow, never pickable.
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(400, 400),
    new THREE.MeshStandardMaterial({ color: 0x33373f, roughness: 1 }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.3;
  floor.receiveShadow = true;
  floor.raycast = () => {};
  floor.userData = { kind: 'floor' };
  scene.add(floor);

  const aspect = container.clientWidth / Math.max(container.clientHeight, 1);
  const initialHalfH = 5;
  const camera = new THREE.OrthographicCamera(
    -initialHalfH * aspect,
    initialHalfH * aspect,
    initialHalfH,
    -initialHalfH,
    ORTHO_NEAR,
    ORTHO_FAR,
  );
  camera.position.copy(ISO_DIRECTION).multiplyScalar(CAMERA_DISTANCE).add(new THREE.Vector3(0, 0.5, 0));
  // OrthographicCamera has no aspect in its constructor; set it here so
  // frameCamera's aspect enforcement is correct before any resize.
  camera.aspect = aspect;

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);

  // OrbitControls: left-drag rotate, right-drag pan, wheel zoom.
  // In ortho, wheel zoom changes camera.zoom, not distance, so the
  // limits are minZoom/maxZoom (minDistance/maxDistance are meaningless
  // here and are not set). enableDamping smooths all three; update()
  // is called every frame.
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.enableRotate = true;
  controls.enableZoom = true;
  controls.enablePan = true;
  controls.mouseButtons = {
    LEFT: THREE.MOUSE.ROTATE,
    MIDDLE: THREE.MOUSE.DOLLY,
    RIGHT: THREE.MOUSE.PAN,
  };
  controls.touches = {
    ONE: THREE.TOUCH.ROTATE,
    TWO: THREE.TOUCH.DOLLY_PAN,
  };
  controls.minZoom = 0.5;
  controls.maxZoom = 8;
  controls.minPolarAngle = 0.15; // never flip under the slab from the top
  controls.maxPolarAngle = Math.PI / 2 - 0.02; // stay above the slab
  controls.screenSpacePanning = false; // pan moves in the ground plane
  controls.target.set(0, 0.5, 0);

  const components = new Map();
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let selectedId = null;
  let onPickHandler = onPick;
  let disposed = false;
  let rafId = 0;

  // -- Inspect explode (visual only: writes group.position.y and
  //    nothing else; never touches nodes, values or verdicts). --------
  let explodeTarget = false; // where the tween is headed
  let explodeAnim = null; // { from: Map<group, y>, to: Map<group, y>, start, target }

  function explodeGroups() {
    const groups = [...components.values()].filter(Boolean);
    return groups.map((group, order) => ({
      group,
      index: Number.isFinite(group.userData.explodeIndex)
        ? group.userData.explodeIndex
        : order,
      baseY: Number.isFinite(group.userData.baseY) ? group.userData.baseY : 0,
    }));
  }

  function snapExplode(target) {
    for (const { group, index, baseY } of explodeGroups()) {
      group.position.y = baseY + (target ? explodeOffset(index) : 0);
    }
  }

  function setExploded(next) {
    const target = next === true;
    if (target === explodeTarget && !explodeAnim) return target;
    explodeTarget = target;
    if (prefersReducedMotion()) {
      explodeAnim = null;
      snapExplode(target);
      return target;
    }
    const from = new Map();
    const to = new Map();
    for (const { group, index, baseY } of explodeGroups()) {
      from.set(group, group.position.y);
      to.set(group, baseY + (target ? explodeOffset(index) : 0));
    }
    explodeAnim = {
      from,
      to,
      start: typeof performance !== 'undefined' ? performance.now() : 0,
      target,
    };
    return target;
  }

  // Advance the explode tween to time `now` (ms, same clock as
  // performance.now()). Called every frame from the animate loop; also
  // exposed so a caller with its own tick (main3d.js) can drive it —
  // calling it twice for one frame is harmless (same `now`, same Y).
  // Toggling mid-tween reverses from the current Y, never snaps.
  function stepExplode(now) {
    if (!explodeAnim) return;
    const t = Math.min(1, Math.max(0, (now - explodeAnim.start) / EXPLODE_DURATION_MS));
    const e = easeInOutCubic(t);
    for (const [group, fromY] of explodeAnim.from) {
      const toY = explodeAnim.to.get(group);
      if (toY === undefined) continue;
      if (group.parent === null && !components.has(group.userData.componentId)) continue;
      group.position.y = fromY + (toY - fromY) * e;
    }
    if (t >= 1) explodeAnim = null;
  }

  function resetExplode() {
    explodeTarget = false;
    explodeAnim = null;
    snapExplode(false);
  }

  function setSelected(componentId) {
    // State only. The old 1.12x scale pop is gone: it would break the
    // flat blueprint read and fight the explode tween. Selection's
    // single visual signal is the BoxHelper outline in interaction.js.
    selectedId = componentId && components.has(componentId) ? componentId : null;
  }

  function pickAt(clientX, clientY) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects([...components.values()], true);
    const hit = hits.find((h) => h.object.userData.componentId);
    const componentId = hit ? hit.object.userData.componentId : null;
    setSelected(componentId);
    if (onPickHandler) onPickHandler(componentId);
    return componentId;
  }

  // Click (not drag): only pick when the pointer barely moved, so an
  // orbit drag does not select a component by accident.
  let downX = 0;
  let downY = 0;
  function onPointerDown(event) {
    downX = event.clientX;
    downY = event.clientY;
  }
  function onPointerUp(event) {
    const moved = Math.hypot(event.clientX - downX, event.clientY - downY);
    if (moved <= 5) pickAt(event.clientX, event.clientY);
  }
  renderer.domElement.addEventListener('pointerdown', onPointerDown);
  renderer.domElement.addEventListener('pointerup', onPointerUp);

  function resize() {
    const w = container.clientWidth;
    const h = Math.max(container.clientHeight, 1);
    const nextAspect = w / h;
    camera.aspect = nextAspect;
    // Re-apply the stored frustum height for the new aspect; only the
    // aspect changes on resize. A user zoom is preserved (zoom is not
    // reset here) and the box is NOT re-fit.
    const halfH = Number.isFinite(view._orthoHalfH) ? view._orthoHalfH : initialHalfH;
    camera.left = -halfH * nextAspect;
    camera.right = halfH * nextAspect;
    camera.top = halfH;
    camera.bottom = -halfH;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }
  function animate(now) {
    if (disposed) return;
    rafId = requestAnimationFrame(animate);
    controls.update();
    stepExplode(typeof now === 'number' ? now : (typeof performance !== 'undefined' ? performance.now() : 0));
    renderer.render(scene, camera);
  }
  function dispose() {
    disposed = true;
    cancelAnimationFrame(rafId);
    resizeObserver.disconnect();
    window.removeEventListener('resize', resize);
    renderer.domElement.removeEventListener('pointerdown', onPointerDown);
    renderer.domElement.removeEventListener('pointerup', onPointerUp);
    controls.dispose();
    clearComponents(view);
    renderer.dispose();
    if (renderer.domElement.parentElement === container) {
      container.removeChild(renderer.domElement);
    }
  }

  const view = {
    scene,
    camera,
    renderer,
    controls,
    components,
    raycaster,
    get selectedId() {
      return selectedId;
    },
    pickAt,
    setOnPick(fn) {
      onPickHandler = typeof fn === 'function' ? fn : null;
    },
    setSelected,
    frameCamera(options) {
      return frameCamera(view, options);
    },
    resetToIso() {
      return resetToIso(view);
    },
    setExploded,
    toggleExplode() {
      return setExploded(!explodeTarget);
    },
    isExploded() {
      return explodeTarget;
    },
    stepExplode,
    resetExplode,
    dispose,
  };
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(container);
  window.addEventListener('resize', resize);
  animate();
  return view;
}

/**
 * Add (or replace) one component mesh. The mesh is built by the shared
 * blueprint builders in components.js, so it looks identical to a part
 * built by buildCircuit.
 * @param {object} view - return value of createScene().
 * @param {object} component - { id, type, value, nodes } (IR shape).
 * @param {object} [options]
 * @param {{ x: number, z: number }} [options.position] - explicit ground position;
 *   otherwise components auto-lay-out left to right in insertion order.
 * @param {number} [options.span] - node-to-node distance for the part (default 3).
 * @param {string} [options.verdict] - initial verdict colour (PASS | WARN | STOP).
 * @returns {THREE.Group} the added mesh group.
 */
export function addComponent(view, component, options = {}) {
  if (!view || !view.scene || !view.components) {
    throw new TypeError('addComponent requires a view from createScene()');
  }
  if (!component || typeof component.id !== 'string' || !component.id.trim()) {
    throw new TypeError('addComponent requires a component with a non-empty string id');
  }
  const id = component.id.trim();
  if (view.components.has(id)) removeComponent(view, id);
  const type = normalizeType(component.type);
  const group = buildComponentMesh(
    {
      id,
      type,
      value: component.value ?? null,
      nodes: Array.isArray(component.nodes) ? component.nodes : ['n1', 'n2'],
      color: component.color ?? null,
      ratingWatts: component.rating_watts ?? component.ratingWatts ?? null,
    },
    Number.isFinite(options.span) && options.span > 0 ? options.span : 3,
  );
  group.userData.explodeIndex = view.components.size;
  const pos = layoutPosition(view.components.size, options.position);
  group.position.set(pos.x, GROUND_Y, pos.z);
  view.scene.add(group);
  view.components.set(id, group);
  if (options.verdict) applyVerdictToGroup(group, options.verdict);
  return group;
}

/**
 * Add every component in a netlist (array of IR components, or the
 * `{ id, type, value, nodes }` rows the web UI table produces).
 * Existing meshes are kept unless an id collides, in which case that
 * component is replaced. Returns the added groups.
 */
export function addComponents(view, netlist, options = {}) {
  const rows = Array.isArray(netlist) ? netlist : (netlist?.components ?? []);
  return rows.map((row) => addComponent(view, row, options));
}

function disposeObject(root) {
  root.traverse((child) => {
    if (child.isMesh || child.isLine || child.isSprite) {
      child.geometry?.dispose?.();
      const materials = Array.isArray(child.material) ? child.material : [child.material];
      for (const m of materials) {
        if (m && typeof m.dispose === 'function') m.dispose();
      }
    }
  });
}

/**
 * Remove a component mesh by id. Returns true if one was removed.
 */
export function removeComponent(view, componentId) {
  if (!view || !view.components) return false;
  const id = String(componentId);
  const group = view.components.get(id);
  if (!group) return false;
  view.scene.remove(group);
  disposeObject(group);
  view.components.delete(id);
  if (view.selectedId === id) view.setSelected(null);
  return true;
}

/** Remove every component mesh. Returns the number removed. */
export function clearComponents(view) {
  if (!view || !view.components) return 0;
  if (typeof view.resetExplode === 'function') view.resetExplode();
  const ids = [...view.components.keys()];
  ids.forEach((id) => removeComponent(view, id));
  return ids.length;
}

/** Get the mesh group for a component id, or null. */
export function getComponentMesh(view, componentId) {
  return view?.components?.get(String(componentId)) ?? null;
}

/**
 * Update verdict colours.
 *
 * Accepts either:
 *   updateVerdictColors(view, { R1: 'PASS', D1: 'STOP' })  (per component), or
 *   updateVerdictColors(view, 'STOP', ['R1', 'D1'])         (one verdict for
 *     the listed ids; omit the list to colour every component), or
 *   updateVerdictColors(view, findings) where findings is an array of
 *     { severity, component_ids } from a /check report — the worst
 *     severity per component wins (STOP > WARN > INFO; INFO never colours,
 *     it resolves to neutral blue).
 * Unknown ids are ignored. Returns the map of id -> applied verdict.
 * Severity changes edges + glow only: fills stay white / pale-blue.
 */
export function updateVerdictColors(view, verdicts, componentIds) {
  if (!view || !view.components) return {};
  const applied = {};

  const applyOne = (id, verdict) => {
    const group = view.components.get(String(id));
    if (!group) return;
    const v = normalizeVerdict(verdict);
    applyVerdictToGroup(group, v);
    applied[String(id)] = v;
  };

  if (typeof verdicts === 'string') {
    const ids = Array.isArray(componentIds)
      ? componentIds
      : [...view.components.keys()];
    ids.forEach((id) => applyOne(id, verdicts));
    return applied;
  }

  if (Array.isArray(verdicts)) {
    // Findings array: worst severity per component wins.
    const rank = { STOP: 0, WARN: 1, INFO: 2 };
    const worst = new Map();
    for (const finding of verdicts) {
      if (!finding) continue;
      const sev = normalizeVerdict(finding.severity || finding.level || finding.verdict);
      const ids = finding.component_ids || finding.componentIds || (finding.component ? [finding.component] : []);
      for (const id of ids) {
        const prev = worst.get(String(id));
        if (!prev || (rank[sev] ?? 3) < (rank[prev] ?? 3)) worst.set(String(id), sev);
      }
    }
    worst.forEach((sev, id) => applyOne(id, sev));
    return applied;
  }

  if (verdicts && typeof verdicts === 'object') {
    Object.entries(verdicts).forEach(([id, verdict]) => applyOne(id, verdict));
    return applied;
  }

  return applied;
}

/** Set a single component's verdict colour. Returns true if found. */
export function setComponentVerdict(view, componentId, verdict) {
  const group = view?.components?.get(String(componentId));
  if (!group) return false;
  applyVerdictToGroup(group, verdict);
  return true;
}

/** Reset every component to the neutral (no verdict) colour: blue edges, no glow. */
export function resetVerdictColors(view) {
  if (!view || !view.components) return;
  view.components.forEach((group) => applyVerdictToGroup(group, 'UNKNOWN'));
}
