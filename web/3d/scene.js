// Netproof 3D scene — Three.js module (frontend-only, no backend deps).
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

// Verdict -> mesh colour. Matches the 2D verdict banner in web/index.html
// (PASS green #1a7f37, WARN amber #d97706, STOP red #b00020), brightened
// slightly so emissive meshes read on the dark scene.
export const VERDICT_COLORS = Object.freeze({
  PASS: 0x2ea043,
  WARN: 0xd97706,
  STOP: 0xe5484d,
  INFO: 0x4da3ff,
  UNKNOWN: 0x8b949e,
});

// Default (no verdict yet) body colour per component type.
export const COMPONENT_COLORS = Object.freeze({
  resistor: 0xc9a227,
  capacitor: 0x4da3ff,
  led: 0xff4d4d,
  diode: 0xff8a5c,
  voltage_source: 0x3ddc84,
  current_source: 0xb388ff,
  default: 0x8b949e,
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

function makeMaterial(color, { emissive = 0x000000, emissiveIntensity = 0 } = {}) {
  return new THREE.MeshStandardMaterial({
    color,
    emissive,
    emissiveIntensity,
    roughness: 0.55,
    metalness: 0.25,
  });
}

function box(w, h, d, color, x = 0, y = 0, z = 0) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), makeMaterial(color));
  mesh.position.set(x, y, z);
  mesh.castShadow = true;
  return mesh;
}

function cylinder(rt, rb, h, color, x = 0, y = 0, z = 0, radialSegments = 24) {
  const mesh = new THREE.Mesh(
    new THREE.CylinderGeometry(rt, rb, h, radialSegments),
    makeMaterial(color),
  );
  mesh.position.set(x, y, z);
  mesh.castShadow = true;
  return mesh;
}

// One schematic-style mesh group per component type. Geometry is purely
// visual; it encodes no electrical value.
function buildComponentMesh(component) {
  const type = normalizeType(component.type);
  const color = COMPONENT_COLORS[type] ?? COMPONENT_COLORS.default;
  const group = new THREE.Group();
  const bodyY = 0.55;

  if (type === 'resistor') {
    const body = cylinder(0.28, 0.28, 1.1, color, 0, bodyY, 0);
    body.rotation.z = Math.PI / 2;
    group.add(body);
    const lead = cylinder(0.05, 0.05, 0.7, 0xcccccc, -0.9, bodyY, 0, 8);
    lead.rotation.z = Math.PI / 2;
    const lead2 = lead.clone();
    lead2.position.x = 0.9;
    group.add(lead, lead2);
  } else if (type === 'capacitor') {
    group.add(box(0.12, 0.9, 0.7, color, -0.18, bodyY, 0));
    group.add(box(0.12, 0.9, 0.7, 0xdddddd, 0.18, bodyY, 0));
    const lead = cylinder(0.05, 0.05, 0.5, 0xcccccc, -0.55, bodyY, 0, 8);
    lead.rotation.z = Math.PI / 2;
    const lead2 = lead.clone();
    lead2.position.x = 0.55;
    group.add(lead, lead2);
  } else if (type === 'led' || type === 'diode') {
    const cone = new THREE.Mesh(
      new THREE.ConeGeometry(0.42, 0.8, 24),
      makeMaterial(color, { emissive: color, emissiveIntensity: 0.35 }),
    );
    cone.rotation.z = -Math.PI / 2;
    cone.position.set(0, bodyY, 0);
    cone.castShadow = true;
    group.add(cone);
    group.add(box(0.1, 0.7, 0.7, 0xdddddd, 0.5, bodyY, 0));
    const lead = cylinder(0.05, 0.05, 0.5, 0xcccccc, -0.7, bodyY, 0, 8);
    lead.rotation.z = Math.PI / 2;
    group.add(lead);
    if (type === 'led') {
      const dome = new THREE.Mesh(
        new THREE.SphereGeometry(0.3, 24, 16),
        makeMaterial(color, { emissive: color, emissiveIntensity: 0.6 }),
      );
      dome.position.set(-0.45, bodyY + 0.45, 0);
      dome.castShadow = true;
      group.add(dome);
    }
  } else if (type === 'voltage_source') {
    const cell = cylinder(0.35, 0.35, 1.0, color, 0, bodyY, 0);
    group.add(cell);
    group.add(cylinder(0.36, 0.36, 0.08, 0xdddddd, 0, bodyY + 0.54, 0));
    const lead = cylinder(0.05, 0.05, 0.5, 0xcccccc, 0, bodyY + 0.8, 0, 8);
    const lead2 = lead.clone();
    lead2.position.y = bodyY - 0.8;
    group.add(lead, lead2);
  } else if (type === 'current_source') {
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(0.45, 0.12, 12, 32),
      makeMaterial(color),
    );
    ring.position.set(0, bodyY, 0);
    ring.castShadow = true;
    group.add(ring);
    const arrow = new THREE.Mesh(
      new THREE.ConeGeometry(0.16, 0.45, 12),
      makeMaterial(0xffffff),
    );
    arrow.position.set(0, bodyY, 0);
    group.add(arrow);
  } else {
    group.add(box(0.9, 0.9, 0.9, color, 0, bodyY, 0));
  }

  // Small feet so every part sits on the ground plane.
  const footL = box(0.08, bodyY, 0.08, 0x666666, -0.85, bodyY / 2, 0);
  const footR = box(0.08, bodyY, 0.08, 0x666666, 0.85, bodyY / 2, 0);
  group.add(footL, footR);

  group.userData.componentId = component.id;
  group.userData.type = type;
  group.userData.value = component.value ?? null;
  group.userData.nodes = Array.isArray(component.nodes)
    ? component.nodes
    : [];
  group.userData.verdict = 'UNKNOWN';
  group.userData.baseColor = color;
  group.traverse((child) => {
    if (child.isMesh) {
      child.userData.componentId = component.id;
      child.userData.rootGroup = group;
    }
  });
  return group;
}

function applyVerdictToGroup(group, verdict) {
  const v = normalizeVerdict(verdict);
  const color = VERDICT_COLORS[v];
  group.userData.verdict = v;
  group.traverse((child) => {
    if (!child.isMesh || !child.material || !child.material.emissive) return;
    // Tint the body toward the verdict colour; keep metal leads neutral.
    if (child.material.color.getHex() === 0xcccccc
      || child.material.color.getHex() === 0xdddddd
      || child.material.color.getHex() === 0x666666
      || child.material.color.getHex() === 0xffffff) {
      return;
    }
    child.material.emissive.setHex(color);
    child.material.emissiveIntensity = v === 'UNKNOWN' ? 0 : 0.45;
  });
}

function layoutPosition(index, position) {
  if (position && Number.isFinite(position.x) && Number.isFinite(position.z)) {
    return { x: position.x, z: position.z };
  }
  return { x: (index - 0) * SPACING, z: 0 };
}

/**
 * Frame the camera on the circuit's bounding box.
 *
 * Computes the Box3 union of every mesh in `view.components`, puts the
 * OrbitControls target on its centre, and moves the camera along its
 * current viewing direction (so an orbit angle the user chose is kept)
 * to the smallest distance at which every corner of the box fits inside
 * the view frustum (vertical and horizontal FOV both honoured), times
 * `options.margin` (default 1.15). Returns false (and moves nothing)
 * when there are no components, so an empty scene keeps its view.
 * Callers: main3d.js buildDisplayed() — i.e. on load and on every
 * setNetlist / history-round rebuild. Also exposed as view.frameCamera.
 */
export function frameCamera(view, options = {}) {
  if (!view || !view.camera || !view.controls
    || !view.components || !view.components.size) return false;
  const box = new THREE.Box3();
  let hasBox = false;
  view.components.forEach((group) => {
    if (!group) return;
    const b = new THREE.Box3().setFromObject(group);
    if (b.isEmpty()) return;
    box.union(b);
    hasBox = true;
  });
  if (!hasBox || box.isEmpty()) return false;

  const center = box.getCenter(new THREE.Vector3());
  const margin = Number.isFinite(options.margin) && options.margin > 0
    ? options.margin
    : 1.15;

  // Keep the current viewing direction; only distance/target change.
  const direction = view.camera.position.clone().sub(view.controls.target);
  if (direction.lengthSq() < 1e-6) direction.set(0.5, 0.45, 0.75);
  direction.normalize();

  const vHalf = THREE.MathUtils.degToRad(view.camera.fov / 2);
  const hHalf = Math.atan(
    Math.tan(vHalf) * Math.max(view.camera.aspect || 1, 0.01),
  );
  const right = new THREE.Vector3().crossVectors(direction, new THREE.Vector3(0, 1, 0));
  if (right.lengthSq() < 1e-6) right.set(1, 0, 0); // looking straight down
  right.normalize();
  const up = new THREE.Vector3().crossVectors(right, direction).normalize();

  // Smallest distance at which every box corner is inside the frustum:
  // a corner at offset v needs depth >= |v.right| / tan(hHalf) and
  // >= |v.up| / tan(vHalf), where depth = distance - v.along (corners
  // toward the camera are nearer, so they need more distance).
  // A bounding sphere would force the long, flat LED circuit to fit its
  // 6-unit length into the viewport height; the corner fit fills the
  // width instead.
  let distance = 0;
  for (const x of [box.min.x, box.max.x]) {
    for (const y of [box.min.y, box.max.y]) {
      for (const z of [box.min.z, box.max.z]) {
        const v = new THREE.Vector3(x, y, z).sub(center);
        const along = v.dot(direction);
        distance = Math.max(
          distance,
          Math.abs(v.dot(right)) / Math.tan(hHalf) + along,
          Math.abs(v.dot(up)) / Math.tan(vHalf) + along,
        );
      }
    }
  }
  distance *= margin;
  const minD = view.controls.minDistance ?? 2;
  const maxD = view.controls.maxDistance ?? 40;
  distance = THREE.MathUtils.clamp(distance, minD, maxD);

  view.controls.target.copy(center);
  view.camera.position.copy(center).addScaledVector(direction, distance);
  view.controls.update();
  return true;
}

/**
 * Create the Three.js scene inside `container`.
 *
 * @param {HTMLElement} container - element the renderer canvas is appended to.
 * @param {object} [options]
 * @param {(componentId: string | null) => void} [options.onPick] - called
 *   with the picked component id, or null when empty space is clicked.
 * @param {number} [options.background] - scene background colour (default dark).
 * @returns {{ scene, camera, renderer, controls, components: Map,
 *   raycaster, selectedId, pickAt, setOnPick, frameCamera, dispose }}
 *   `components` maps component id -> mesh group. The render loop runs
 *   until `dispose()` is called. `frameCamera()` fits the camera to
 *   the bounding box of every component (see frameCamera).
 */
export function createScene(container, options = {}) {
  if (!container || typeof container.appendChild !== 'function') {
    throw new TypeError('createScene requires a DOM container element');
  }
  const onPick = typeof options.onPick === 'function' ? options.onPick : null;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(options.background ?? 0x0b0e14);
  scene.fog = new THREE.Fog(0x0b0e14, 18, 42);

  const camera = new THREE.PerspectiveCamera(
    50,
    container.clientWidth / Math.max(container.clientHeight, 1),
    0.1,
    200,
  );
  camera.position.set(6, 6, 9);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);

  // OrbitControls: left-drag rotate, right-drag pan, wheel zoom.
  // enableDamping smooths all three; update() is called every frame.
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
  controls.minDistance = 2;
  controls.maxDistance = 40;
  controls.maxPolarAngle = Math.PI / 2 - 0.02; // stay above the ground plane
  controls.target.set(0, 0.5, 0);

  // Lights: soft ambient fill + key directional with shadows.
  const ambient = new THREE.AmbientLight(0xffffff, 0.55);
  scene.add(ambient);
  const directional = new THREE.DirectionalLight(0xffffff, 1.4);
  directional.position.set(6, 10, 4);
  directional.castShadow = true;
  directional.shadow.mapSize.set(2048, 2048);
  directional.shadow.camera.left = -12;
  directional.shadow.camera.right = 12;
  directional.shadow.camera.top = 12;
  directional.shadow.camera.bottom = -12;
  scene.add(directional);
  const rim = new THREE.DirectionalLight(0x4da3ff, 0.35);
  rim.position.set(-6, 4, -6);
  scene.add(rim);

  // Dark grid ground: large ground plane + grid lines.
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(60, 60),
    new THREE.MeshStandardMaterial({ color: 0x11151d, roughness: 0.95, metalness: 0 }),
  );
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = GROUND_Y;
  ground.receiveShadow = true;
  scene.add(ground);
  const grid = new THREE.GridHelper(40, 40, 0x2a3342, 0x1a2230);
  grid.position.y = GROUND_Y + 0.01;
  scene.add(grid);

  const components = new Map();
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let selectedId = null;
  let onPickHandler = onPick;
  let disposed = false;
  let rafId = 0;

  function setSelected(componentId) {
    if (selectedId && components.has(selectedId)) {
      const prev = components.get(selectedId);
      prev.scale.setScalar(1);
    }
    selectedId = componentId;
    if (componentId && components.has(componentId)) {
      components.get(componentId).scale.setScalar(1.12);
    }
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
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(container);
  window.addEventListener('resize', resize);

  function animate() {
    if (disposed) return;
    rafId = requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }
  animate();

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
    dispose,
  };
  return view;
}

/**
 * Add (or replace) one component mesh.
 * @param {object} view - return value of createScene().
 * @param {object} component - { id, type, value, nodes } (IR shape).
 * @param {object} [options]
 * @param {{ x: number, z: number }} [options.position] - explicit ground position;
 *   otherwise components auto-lay-out left to right in insertion order.
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
  const group = buildComponentMesh({ ...component, id });
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

/**
 * Remove a component mesh by id. Returns true if one was removed.
 */
export function removeComponent(view, componentId) {
  if (!view || !view.components) return false;
  const id = String(componentId);
  const group = view.components.get(id);
  if (!group) return false;
  view.scene.remove(group);
  group.traverse((child) => {
    if (child.isMesh) {
      child.geometry?.dispose?.();
      if (Array.isArray(child.material)) child.material.forEach((m) => m.dispose?.());
      else child.material?.dispose?.();
    }
  });
  view.components.delete(id);
  if (view.selectedId === id) view.setSelected(null);
  return true;
}

/** Remove every component mesh. Returns the number removed. */
export function clearComponents(view) {
  if (!view || !view.components) return 0;
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
 *     severity per component wins (STOP > WARN > INFO).
 * Unknown ids are ignored. Returns the map of id -> applied verdict.
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

/** Reset every component to the neutral (no verdict) colour. */
export function resetVerdictColors(view) {
  if (!view || !view.components) return;
  view.components.forEach((group) => applyVerdictToGroup(group, 'UNKNOWN'));
}
