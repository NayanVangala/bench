// Netproof 3D components — BLUEPRINT render (wholesale rebuild).
//
// Meshes built from a netlist (frontend-only). This file owns the whole
// blueprint look of the circuit: white / pale-blue MeshBasicMaterial
// fills, blue EdgesGeometry outlines, orthogonal blue wire ribbons on a
// raised slab, dimension lines, and one soft-glow sprite per component
// (severity only; hidden until a verdict colours it).
//
// Usage (from a page that loads this as an ES module):
//   import { buildCircuit } from './3d/components.js';
//   const { group, nodePositions, meshesById } = buildCircuit(netlist);
//   scene.add(group); // caller owns renderer / camera / controls (see 3d/scene.js)
//
// Netlist shape mirrors the backend IR (backend/netproof/ir.py):
//   [{ id, type, value, nodes: [n1, n2], rating_watts?, color? }, ...]
//   or { components: [...] }. `nodes` also accepts the "n1 n2" string form
//   used by the netlist table in web/index.html.
//   type is one of: resistor | capacitor | led | diode | voltage_source
//                  | current_source. Unknown types get a generic box.
//   LED colour comes from the component's `color` field ("red" default,
//   per ir.py); diode/LED anode is nodes[0], cathode is nodes[1];
//   a source's nodes are [+, -].
//
// Geometry only: every size below is a display-only scene unit derived
// from the netlist structure (node count, connectivity, component type,
// value digits for resistor bands, LED colour). No voltages, currents,
// or powers are read here and none are invented — those live in the
// backend simulation results, not in this file.
//
// Blueprint blue is #0B3D91 ONLY (coordinator decision 2026-11-15):
// every edge, wire, node dot and dimension line uses it. Severity edge
// colours are NOT this blue — they live in scene.js VERDICT_COLORS
// (STOP #d92d20, WARN #92400E, PASS #1a7f37) and must change together
// with the matching tables in overlay.js and history.js (history.js
// WARN is #92400E at lines 92-93).
//
// Layout note: layoutNodes() still returns the grid in the XY plane
// exactly as before (topology untouched). buildCircuit() remaps that
// grid onto the ground plane — (x, y, 0) becomes (x, 0, y) — so parts
// stand on the slab, legs point down at it, wires run flat on it, and
// dragging on the y=0 ground plane works. The returned nodePositions
// are the ground-plane positions, so wires, slab and flow dots all
// agree on one coordinate frame.
//
// Three.js comes from the bare 'three' specifier, resolved by the host
// page's import map to unpkg three@0.160.0 — the same instance every
// other web/3d/ module uses. RoundedBoxGeometry comes from the same
// map's 'three/addons/' prefix. Do NOT import three from a direct CDN
// URL here: a second URL would load a second THREE instance.

import * as THREE from 'three';
import { partModel, MODEL_MM } from './models.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

export const GRID_X = 3; // node column spacing (scene units)
export const GRID_Y = 2; // node row spacing (scene units)
export const GROUND_NODE = '0';

// Blueprint palette (single source of truth for this file).
export const BLUEPRINT_BLUE = 0x0b3d91; // #0B3D91 — the ONLY blueprint blue
export const BLUEPRINT_EDGE = BLUEPRINT_BLUE;
export const BLUEPRINT_FILL = 0xc9ced6; // neutral body fill (unknown parts)
export const BLUEPRINT_FILL_2 = 0xdbeafe; // pale-blue secondary fill
export const BLUEPRINT_INK = 0x1e3a5f; // 3D-adjacent text (labels, dimensions)
// Colourised bench palette (lit MeshStandardMaterial, see blueprintMaterial).
export const PART_COLORS = Object.freeze({
  metal: 0xb9bec7, // leads, legs
  resistorBody: 0xe6cf9f, // beige carbon-film body
  sourceBody: 0xf29a2e, // battery pack body
  sourceTop: 0x2a2d33, // battery cap
  capPlateA: 0x2f6fde,
  capPlateB: 0x8db8ff,
  diodeBody: 0x2b2d31,
  currentSource: 0xf2c94c,
  mat: 0x2a59d6, // bench mat (slab top)
  matRim: 0xf3f5f9, // white mat rim
  nodeDot: 0xf4f6fa,
  ground: 0x1d2026,
  dimension: 0xc7d2e6,
});
// Wire colours: ground nets dark, the rest cycle red / yellow / green / white.
export const WIRE_COLORS = Object.freeze([0xe5484d, 0xf5c542, 0x34c26a, 0xf4f6fa]);
export const WIRE_GROUND = 0x1d2026;
export const BLUEPRINT_WIRE = BLUEPRINT_BLUE;

// Slab: raised sheet the circuit sits on. Size is the circuit bounding
// box + SLAB_PAD on each axis; top face is exactly y = 0.
export const SLAB_PAD = 1.6;
export const SLAB_HEIGHT = 0.18;
export const SLAB_RADIUS = 0.12;

// Inspect explode: staggered vertical offsets, eased over 300 ms.
export const EXPLODE_BASE = 0.9;
export const EXPLODE_STEP = 0.18;
export const EXPLODE_DURATION_MS = 300;

/** Vertical explode offset for the component at netlist `index`. */
export function explodeOffset(index) {
  return EXPLODE_BASE + EXPLODE_STEP * Math.max(0, index | 0);
}

/** easeInOutCubic: t<0.5 ? 4t^3 : 1-((-2t+2)^3)/2. */
export function easeInOutCubic(t) {
  const x = Math.min(1, Math.max(0, t));
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
}

export const COMPONENT_TYPES = Object.freeze([
  'resistor',
  'capacitor',
  'led',
  'diode',
  'voltage_source',
  'current_source',
]);

// Standard resistor colour code: digit -> hex colour.
const RESISTOR_DIGIT_COLORS = Object.freeze([
  0x1a1a1a, // 0 black
  0x7b3f00, // 1 brown
  0xd43d2a, // 2 red
  0xe67e22, // 3 orange
  0xf1c40f, // 4 yellow
  0x27ae60, // 5 green
  0x2471a3, // 6 blue
  0x8e44ad, // 7 violet
  0x808080, // 8 grey
  0xf8f8f8, // 9 white
]);
const GOLD = 0xffd700;
const SILVER = 0xc0c0c0;

// LED colour name -> hex. Unknown names fall back to red (ir.py default).
export const LED_COLORS = Object.freeze({
  red: 0xff2a2a,
  green: 0x2aff5a,
  blue: 0x2a7fff,
  white: 0xffffff,
  yellow: 0xffe62a,
  orange: 0xff8a2a,
  amber: 0xffb02a,
});

export function ledColorHex(color) {
  if (typeof color === 'string') {
    const name = color.trim().toLowerCase();
    if (name in LED_COLORS) return LED_COLORS[name];
    if (/^#[0-9a-f]{6}$/i.test(name)) return parseInt(name.slice(1), 16);
  }
  return LED_COLORS.red;
}

// Resistor bands from the resistance value (standard colour code):
// [1st significant digit, 2nd significant digit, multiplier, tolerance].
// Tolerance is always gold here: it is a display convention, not a
// claim about the physical part (ir.py carries no tolerance field).
export function resistorBandColors(value) {
  if (!Number.isFinite(value) || value <= 0) {
    return [RESISTOR_DIGIT_COLORS[8], RESISTOR_DIGIT_COLORS[8], RESISTOR_DIGIT_COLORS[8], GOLD];
  }
  const exp = Math.floor(Math.log10(value));
  const mantissa = value / Math.pow(10, exp - 1); // in [10, 100)
  const d1 = Math.floor(mantissa / 10);
  const d2 = Math.floor(mantissa % 10);
  const mult = exp - 1;
  const multColor =
    mult >= 0 && mult <= 9
      ? RESISTOR_DIGIT_COLORS[mult]
      : mult === -1
        ? GOLD
        : mult === -2
          ? SILVER
          : RESISTOR_DIGIT_COLORS[8];
  return [RESISTOR_DIGIT_COLORS[d1], RESISTOR_DIGIT_COLORS[d2], multColor, GOLD];
}

// ---------------------------------------------------------------------------
// Normalisation
// ---------------------------------------------------------------------------

function normalizeNodes(nodes) {
  if (Array.isArray(nodes)) return nodes.map((n) => String(n).trim());
  if (typeof nodes === 'string') return nodes.split(/[\s,]+/).filter(Boolean);
  return [];
}

export function normalizeNetlist(netlist) {
  const raw =
    Array.isArray(netlist) ? netlist : netlist && Array.isArray(netlist.components) ? netlist.components : null;
  if (!raw) throw new TypeError('buildCircuit expects a component array or { components: [...] }');
  return raw.map((c, i) => {
    const nodes = normalizeNodes(c.nodes);
    if (!c || typeof c.id !== 'string' || !c.id.trim()) {
      throw new TypeError(`netlist[${i}] is missing a non-empty string id`);
    }
    if (nodes.length !== 2 || !nodes[0] || !nodes[1]) {
      throw new TypeError(`netlist[${i}] (${c.id}) needs exactly two nodes, got ${JSON.stringify(c.nodes)}`);
    }
    if (nodes[0] === nodes[1]) {
      throw new TypeError(`netlist[${i}] (${c.id}) has the same node twice: ${nodes[0]}`);
    }
    const value = typeof c.value === 'number' ? c.value : Number(c.value);
    return {
      id: c.id.trim(),
      type: String(c.type || '').trim().toLowerCase(),
      value: Number.isFinite(value) ? value : null,
      nodes,
      color: typeof c.color === 'string' && c.color.trim() ? c.color.trim() : null,
      ratingWatts:
        typeof c.rating_watts === 'number'
          ? c.rating_watts
          : typeof c.ratingWatts === 'number'
            ? c.ratingWatts
            : null,
    };
  });
}

// ---------------------------------------------------------------------------
// Layout: deterministic grid by connectivity.
//   ground ("0") at the origin; source + terminals on the left (x < 0);
//   every other node on the right (x > 0), columned by BFS hop distance
//   from ground, rows spread within each column by first-appearance order.
//   Nodes unreachable from ground go in one extra column past the last
//   reachable one. Same netlist in -> same positions out.
//   UNTOUCHED by the blueprint rebuild: topology only, no rendering.
// ---------------------------------------------------------------------------

export function layoutNodes(components) {
  const order = []; // node names in first-appearance order
  const adjacency = new Map(); // node -> Set<node>
  const addEdge = (a, b) => {
    for (const [from, to] of [[a, b], [b, a]]) {
      if (!adjacency.has(from)) adjacency.set(from, new Set());
      adjacency.get(from).add(to);
    }
  };
  for (const comp of components) {
    for (const n of comp.nodes) if (!order.includes(n)) order.push(n);
    addEdge(comp.nodes[0], comp.nodes[1]);
  }
  if (!order.length) return new Map();

  const ground =
    order.find((n) => n === GROUND_NODE) ||
    order.find((n) => n.toLowerCase() === 'gnd') ||
    order[0];

  // BFS hop distance from ground.
  const hops = new Map([[ground, 0]]);
  const queue = [ground];
  while (queue.length) {
    const cur = queue.shift();
    for (const next of adjacency.get(cur) || []) {
      if (!hops.has(next)) {
        hops.set(next, hops.get(cur) + 1);
        queue.push(next);
      }
    }
  }
  const maxHop = Math.max(0, ...hops.values());

  // Nodes that are the + terminal of a source sit on the left.
  const sourcePositive = new Set();
  for (const comp of components) {
    if (comp.type === 'voltage_source' || comp.type === 'current_source') {
      sourcePositive.add(comp.nodes[0]);
    }
  }

  const positions = new Map();
  positions.set(ground, new THREE.Vector3(0, 0, 0));

  const leftNodes = order.filter((n) => n !== ground && sourcePositive.has(n));
  leftNodes.forEach((n, i) => {
    positions.set(n, new THREE.Vector3(-GRID_X, (i - (leftNodes.length - 1) / 2) * GRID_Y, 0));
  });

  const rightNodes = order.filter((n) => n !== ground && !sourcePositive.has(n));
  const byHop = new Map();
  for (const n of rightNodes) {
    const hop = hops.has(n) ? hops.get(n) : maxHop + 1;
    if (!byHop.has(hop)) byHop.set(hop, []);
    byHop.get(hop).push(n);
  }
  for (const [hop, nodes] of [...byHop.entries()].sort((a, b) => a[0] - b[0])) {
    nodes.forEach((n, i) => {
      positions.set(n, new THREE.Vector3(GRID_X * hop, (i - (nodes.length - 1) / 2) * GRID_Y, 0));
    });
  }
  return positions;
}

// Loop layout for single-loop circuits (every node on exactly two parts,
// e.g. battery -> resistor -> LED -> back). Nodes go round a rectangle:
// the source on the left edge, the rest clockwise over top, right and
// bottom. Each part's wire follows the rectangle edge between its two
// nodes, and the part sits on the longest straight run of that wire, so
// parts never overlap and always lie on their own wire. Returns null for
// anything that is not a simple loop (the grid layout handles those).
// Returns { positions: Map<node, Vector3(x, 0, z)>, routes: Map<compId, Vector3[]> }.
export function loopLayout(components) {
  const n = components.length;
  if (n < 3) return null;
  const incident = new Map();
  for (const comp of components) {
    const [a, b] = comp.nodes;
    if (a === b) return null;
    for (const node of [a, b]) {
      if (!incident.has(node)) incident.set(node, []);
      incident.get(node).push(comp);
    }
  }
  if (incident.size !== n) return null;
  for (const list of incident.values()) if (list.length !== 2) return null;

  const names = [...incident.keys()];
  const ground = names.find((x) => x === GROUND_NODE) || names.find((x) => x.toLowerCase() === 'gnd') || names[0];
  const groundComps = incident.get(ground);
  const isSource = (c) => c.type === 'voltage_source' || c.type === 'current_source';
  const first = groundComps.find(isSource) || groundComps[0];
  // Walk the loop: nodes[0] = ground, edges[i] joins nodes[i] -> nodes[i+1].
  const nodes = [ground];
  const edges = [];
  let cur = ground;
  let via = first;
  while (edges.length < n) {
    edges.push(via);
    const next = via.nodes[0] === cur ? via.nodes[1] : via.nodes[0];
    if (edges.length === n) {
      if (next !== ground) return null;
      break;
    }
    if (nodes.includes(next)) return null; // not one loop
    nodes.push(next);
    cur = next;
    via = incident.get(cur).find((c) => c !== via);
  }

  // Rectangle: ground bottom-left, nodes[1] top-left (source on the left
  // edge); remaining nodes evenly spaced along top -> right -> bottom.
  const W = GRID_X * 1.6 * Math.max(1, (n - 1) / 2);
  const H = GRID_Y * 1.6;
  const L = 2 * W + H; // path length TL -> TR -> BR -> BL
  const at = (s) => {
    if (s <= W) return [s, 0];
    if (s <= W + H) return [W, s - W];
    return [W - (s - W - H), H];
  };
  const sOf = [L]; // ground
  for (let i = 1; i < n; i++) sOf.push(((i - 1) / (n - 1)) * L);
  const toV = ([x, z]) => new THREE.Vector3(x - W / 2, 0, z - H / 2);
  const positions = new Map(nodes.map((name, i) => [name, toV(at(sOf[i]))]));
  const routes = new Map();
  edges.forEach((comp, i) => {
    const a = nodes[i];
    const b = nodes[(i + 1) % n];
    let pts;
    if (i === 0) {
      pts = [positions.get(a).clone(), positions.get(b).clone()]; // left edge
    } else {
      const s0 = sOf[i];
      const s1 = sOf[(i + 1) % n];
      pts = [toV(at(s0))];
      for (const corner of [W, W + H]) if (corner > s0 + 1e-9 && corner < s1 - 1e-9) pts.push(toV(at(corner)));
      pts.push(toV(at(s1)));
    }
    if (comp.nodes[0] !== a) pts.reverse(); // route runs nodes[0] -> nodes[1]
    routes.set(comp.id, pts);
  });
  return { positions, routes };
}

// Longest straight run of a route: { a, b } endpoints.
function longestSegment(pts) {
  let best = { a: pts[0], b: pts[1] };
  let bestLen = -1;
  for (let i = 0; i < pts.length - 1; i++) {
    const len = pts[i].distanceTo(pts[i + 1]);
    if (len > bestLen) { bestLen = len; best = { a: pts[i], b: pts[i + 1] }; }
  }
  return best;
}

// ---------------------------------------------------------------------------
// Blueprint mesh helpers. Every builder returns a THREE.Group whose local
// +X axis runs from nodes[0] to nodes[1], centred on the origin, spanning
// `span` (the distance between the two node positions). buildCircuit
// orients it. Local +Y is up off the slab; every body sits on the slab
// top (y = 0), so the group's origin is at slab level.
// ---------------------------------------------------------------------------

/** Lit part material. MeshStandardMaterial so the scene's lights and
 *  shadows shade it, and so severity / selection tint goes through
 *  `emissive` (overlay.js, interaction.js) instead of repainting the body. */
export function blueprintMaterial(fill = BLUEPRINT_FILL, extra = {}) {
  return new THREE.MeshStandardMaterial({ color: fill, roughness: 0.55, metalness: 0.05, ...extra });
}

function mesh(geometry, material, comp) {
  const m = new THREE.Mesh(geometry, material);
  m.userData = userDataFor(comp);
  m.castShadow = true;
  m.receiveShadow = true;
  return m;
}

function userDataFor(comp) {
  const data = { componentId: comp.id, type: comp.type, value: comp.value, nodes: [...comp.nodes] };
  if (comp.color) data.color = comp.color;
  if (comp.ratingWatts != null) data.ratingWatts = comp.ratingWatts;
  return data;
}

// Tag a whole component group (group + every descendant mesh) with the
// component's userData, each mesh getting its own copy.
function tagGroup(group, comp) {
  group.userData = userDataFor(comp);
  group.traverse((child) => {
    if (child.isMesh) child.userData = userDataFor(comp);
  });
}

// Blue outline for one mesh. `materialOrColor` is either the group's
// shared edge material (one per component, so a severity recolour touches
// the whole part with one setHex) or a colour hex for a standalone mesh.
// thresholdAngle 30 keeps crisp box/cylinder rims and drops the faceting
// lines on 24-32 segment cylinders and spheres. Lines never steal a pick.
export function addEdges(meshObj, materialOrColor = BLUEPRINT_EDGE) {
  if (!meshObj || !meshObj.geometry) return null;
  const material =
    materialOrColor && materialOrColor.isMaterial
      ? materialOrColor
      : new THREE.LineBasicMaterial({ color: materialOrColor });
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(meshObj.geometry, 30),
    material,
  );
  edges.raycast = () => {};
  meshObj.add(edges);
  meshObj.userData.edgeMaterial = material;
  return edges;
}

// Cylinder whose axis lies along local X (CylinderGeometry is Y-axis).
function xCylinder(radius, length, material, comp, radiusTop = null) {
  const geo = new THREE.CylinderGeometry(radiusTop ?? radius, radius, length, 32);
  geo.rotateZ(Math.PI / 2);
  return mesh(geo, material, comp);
}

// Cone whose apex points along local +X (ConeGeometry apex is +Y).
function xCone(radius, height, material, comp) {
  const geo = new THREE.ConeGeometry(radius, height, 32);
  geo.rotateZ(-Math.PI / 2);
  return mesh(geo, material, comp);
}

// Horizontal lead: thin white cylinder along local X from x0 to x1 at
// height y. Part of the part itself, not the net wire (see buildWires).
function leadRun(group, x0, x1, y, comp) {
  const len = Math.abs(x1 - x0);
  if (len < 0.02) return null;
  const lead = xCylinder(0.05, len, blueprintMaterial(PART_COLORS.metal, { metalness: 0.6, roughness: 0.35 }), comp);
  lead.position.set((x0 + x1) / 2, y, 0);
  group.add(lead);
  return lead;
}

// Vertical drop: thin white cylinder from height topY down to the slab
// top (y = 0.02), so a lead meets the flat wire ribbon below it.
function drop(group, x, topY, comp) {
  if (topY <= 0.03) return null;
  const len = topY - 0.02;
  const leg = new THREE.Mesh(
    new THREE.CylinderGeometry(0.05, 0.05, len, 8),
    blueprintMaterial(PART_COLORS.metal, { metalness: 0.6, roughness: 0.35 }),
  );
  leg.userData = userDataFor(comp);
  leg.position.set(x, 0.02 + len / 2, 0);
  group.add(leg);
  return leg;
}

// Shared radial-gradient texture for the severity glow sprites: white
// centre fading to transparent, drawn once. The sprite material colour
// tints it; opacity sets the strength.
let glowTexture = null;
function getGlowTexture() {
  if (typeof document === 'undefined') return null;
  if (glowTexture) return glowTexture;
  const canvas = document.createElement('canvas');
  canvas.width = 128;
  canvas.height = 128;
  const ctx = canvas.getContext('2d');
  const gradient = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  gradient.addColorStop(0, 'rgba(255,255,255,1)');
  gradient.addColorStop(0.4, 'rgba(255,255,255,0.45)');
  gradient.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 128, 128);
  glowTexture = new THREE.CanvasTexture(canvas);
  return glowTexture;
}

// Finish a component group: tag it, add blue edges to every mesh (one
// shared edge material per group, stored on the group for severity
// recolouring), and create its hidden severity glow sprite, scaled to
// 1.6x the group's bounding-box size and centred on the group.
// The sprite gets its own geometry clone with an EMPTY bounding box:
// Box3.setFromObject() would otherwise inflate every label anchor and
// camera fit by the 1.6x glow (verified against three@0.160.0 — a sprite
// child expands the box even while visible=false). The position/uv
// attributes the renderer draws from are untouched, and the bounding
// sphere still computes correctly from them.
function finalizeGroup(group, comp) {
  tagGroup(group, comp);
  group.userData.baseY = 0;
  group.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(group);
  const edgeMaterial = new THREE.LineBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.14 });
  group.userData.edgeMaterial = edgeMaterial;
  const meshes = [];
  group.traverse((child) => {
    if (child.isMesh) {
      child.castShadow = true;
      child.receiveShadow = true;
      meshes.push(child);
    }
  });
  for (const m of meshes) addEdges(m, edgeMaterial);
  group.userData.glowSprite = null;
  if (!box.isEmpty()) {
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const texture = getGlowTexture();
    if (texture) {
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
        map: texture,
        color: 0xffffff,
        transparent: true,
        opacity: 0.35,
        depthWrite: false,
        depthTest: true,
      }));
      sprite.geometry = sprite.geometry.clone();
      sprite.geometry.boundingBox = new THREE.Box3().makeEmpty();
      sprite.raycast = () => {};
      sprite.scale.set(1.6 * size.x, 1.6 * size.y, 1);
      sprite.position.copy(center);
      sprite.visible = false;
      group.add(sprite);
      group.userData.glowSprite = sprite;
      group.userData.glowSize = size;
    }
  }
  return group;
}

// ---------------------------------------------------------------------------
// Component builders (blueprint geometry; signatures and spans unchanged)
// ---------------------------------------------------------------------------

function buildResistor(comp, span) {
  const group = new THREE.Group();
  const model = partModel('resistor');
  if (model) {
    // KiCad DIN0207 body (L 6.3 mm, D 2.5 mm) + this part's own value bands.
    group.add(model.object);
    const r = 1.25 * MODEL_MM;
    const len = 6.3 * MODEL_MM;
    const y = model.size.y - r;
    resistorBandColors(comp.value).forEach((color, i) => {
      const band = xCylinder(r * 1.06, len * 0.08, blueprintMaterial(color), comp);
      band.position.set(len * ([-0.3, -0.15, 0, 0.3][i] ?? 0), y, 0);
      group.add(band);
    });
    return group;
  }
  const bodyHalf = Math.min(0.6, span * 0.25);
  const bodyY = 0.32; // body radius, so the body sits on the slab
  const body = xCylinder(0.32, bodyHalf * 2, blueprintMaterial(PART_COLORS.resistorBody), comp);
  body.position.set(0, bodyY, 0);
  group.add(body);
  // Colour bands from the resistance value (standard colour code).
  // Each band is a RAISED ring (radius 0.335 vs body 0.32): a flat stripe
  // would read as paint; a 0.015 proud ring reads as a band. Bands keep
  // their real colours even in blueprint mode; they are data, not decor.
  const bandColors = resistorBandColors(comp.value);
  const bandX = [-0.4, -0.15, 0.1, 0.35].map((f) => f * (bodyHalf / 0.6));
  bandColors.forEach((color, i) => {
    const band = xCylinder(0.335, 0.09, blueprintMaterial(color), comp);
    band.position.set(bandX[i], bodyY, 0);
    group.add(band);
  });
  leadRun(group, -span / 2, -bodyHalf, bodyY, comp);
  leadRun(group, bodyHalf, span / 2, bodyY, comp);
  drop(group, -span / 2, bodyY, comp);
  drop(group, span / 2, bodyY, comp);
  return group;
}

function buildCapacitor(comp, span) {
  const group = new THREE.Group();
  const model = partModel('capacitor');
  if (model) {
    group.add(model.object);
    return group;
  }
  // Two parallel plates, faces normal to the component axis, 0.36 gap
  // between them; it is the symbol. White plate, pale-blue plate.
  const plateY = 0.65; // plate height 1.3, so the plates sit on the slab
  for (const [x, fill] of [[-0.18, PART_COLORS.capPlateA], [0.18, PART_COLORS.capPlateB]]) {
    const plate = mesh(
      new THREE.BoxGeometry(0.07, 1.3, 1.0),
      blueprintMaterial(fill),
      comp,
    );
    plate.position.set(x, plateY, 0);
    group.add(plate);
  }
  leadRun(group, -span / 2, -0.22, plateY, comp);
  leadRun(group, 0.22, span / 2, plateY, comp);
  drop(group, -span / 2, plateY, comp);
  drop(group, span / 2, plateY, comp);
  return group;
}

function buildDiode(comp, span, isLed) {
  const group = new THREE.Group();
  const model = partModel(isLed ? 'led' : 'diode');
  if (model) {
    if (isLed) {
      // Tint the LED's clear epoxy to this part's colour.
      const ledHex = ledColorHex(comp.color || 'red');
      model.object.traverse((o) => {
        const mats = o.isMesh ? [].concat(o.material) : [];
        for (const m of mats) {
          if (m && m.transparent && m.color) { m.color.setHex(ledHex); m.opacity = Math.max(m.opacity, 0.8); }
        }
      });
    }
    group.add(model.object);
    return group;
  }
  if (isLed) {
    // Physical 5 mm LED, lying on its axis: domed top, flange rim,
    // two legs down to the slab. Anode is nodes[0] (local -X).
    const axisY = 0.55; // flange radius, so the flange clears the slab
    const ledHex = ledColorHex(comp.color || 'red');
    // Dome: upper hemisphere, tinted with the LED colour lerped 0.35
    // toward white (pale pink-red for a red LED, not saturated).
    // Sphere pole is +Y; rotate so the dome points along local +X.
    const domeColor = ledHex;
    const domeGeo = new THREE.SphereGeometry(0.45, 32, 16, 0, Math.PI * 2, 0, Math.PI / 2);
    domeGeo.rotateZ(-Math.PI / 2);
    const dome = mesh(domeGeo, blueprintMaterial(domeColor, { roughness: 0.2, transparent: true, opacity: 0.92 }), comp);
    dome.position.set(0, axisY, 0);
    group.add(dome);
    // Flange: flat disc at the dome base, axis along local X.
    const flangeGeo = new THREE.CylinderGeometry(0.55, 0.55, 0.06, 32);
    flangeGeo.rotateZ(Math.PI / 2);
    const flange = mesh(flangeGeo, blueprintMaterial(new THREE.Color(ledHex).multiplyScalar(0.75).getHex()), comp);
    flange.position.set(0, axisY, 0);
    group.add(flange);
    // Legs: vertical cylinders from the flange down to the slab top.
    // Anode leg is the longer one: it reaches y = 0, the cathode leg
    // stops 0.12 short (ends at y = 0.12).
    const anodeLeg = new THREE.Mesh(
      new THREE.CylinderGeometry(0.05, 0.05, axisY, 8),
      blueprintMaterial(PART_COLORS.metal, { metalness: 0.6, roughness: 0.35 }),
    );
    anodeLeg.userData = userDataFor(comp);
    anodeLeg.position.set(-0.35, axisY / 2, 0);
    group.add(anodeLeg);
    const cathodeLeg = new THREE.Mesh(
      new THREE.CylinderGeometry(0.05, 0.05, axisY - 0.12, 8),
      blueprintMaterial(PART_COLORS.metal, { metalness: 0.6, roughness: 0.35 }),
    );
    cathodeLeg.userData = userDataFor(comp);
    cathodeLeg.position.set(0.35, 0.12 + (axisY - 0.12) / 2, 0);
    group.add(cathodeLeg);
    // No triangle/bar glyph: dome + flange + legs already identify the
    // part. No horizontal leads: the legs ARE the leads, and the wire
    // ribbon below meets the foot of each leg.
    return group;
  }
  // Plain diode: triangle (cone) anode -> cathode (+X), dark bar.
  const cone = xCone(0.5, 0.9, blueprintMaterial(PART_COLORS.diodeBody), comp);
  cone.position.set(-0.05, 0.5, 0);
  group.add(cone);
  const bar = mesh(
    new THREE.BoxGeometry(0.08, 1.1, 1.1),
    blueprintMaterial(PART_COLORS.metal),
    comp,
  );
  bar.position.set(0.5, 0.55, 0);
  group.add(bar);
  leadRun(group, -span / 2, -0.5, 0.5, comp);
  leadRun(group, 0.54, span / 2, 0.5, comp);
  drop(group, -span / 2, 0.5, comp);
  drop(group, span / 2, 0.5, comp);
  return group;
}

function buildVoltageSource(comp, span) {
  const group = new THREE.Group();
  // Upright rectangular box, the shape a blueprint reader expects.
  // Nodes[0] (+) is at local -X, nodes[1] (-) at local +X.
  const box = mesh(
    new THREE.BoxGeometry(1.0, 0.9, 0.6),
    blueprintMaterial(PART_COLORS.sourceBody),
    comp,
  );
  box.position.set(0, 0.45, 0);
  group.add(box);
  // + terminal: small raised pale-copper box on the -X face, with the
  // red + glyph mounted on its outer face.
  const plusTerm = mesh(
    new THREE.BoxGeometry(0.08, 0.3, 0.3),
    blueprintMaterial(0xd8a078),
    comp,
  );
  plusTerm.position.set(-0.54, 0.45, 0);
  group.add(plusTerm);
  const plusMat = blueprintMaterial(0xd43d2a);
  const plusV = mesh(new THREE.BoxGeometry(0.05, 0.42, 0.08), plusMat, comp);
  plusV.position.set(-0.6, 0.45, 0);
  const plusH = mesh(new THREE.BoxGeometry(0.05, 0.08, 0.42), plusMat, comp);
  plusH.position.set(-0.6, 0.45, 0);
  group.add(plusV, plusH);
  // - terminal: small raised dark-grey box on the +X face, single
  // horizontal minus bar on its outer face.
  const minusTerm = mesh(
    new THREE.BoxGeometry(0.08, 0.3, 0.3),
    blueprintMaterial(0x555555),
    comp,
  );
  minusTerm.position.set(0.54, 0.45, 0);
  group.add(minusTerm);
  const minusBar = mesh(
    new THREE.BoxGeometry(0.05, 0.08, 0.42),
    blueprintMaterial(0x333333),
    comp,
  );
  minusBar.position.set(0.6, 0.45, 0);
  group.add(minusBar);
  leadRun(group, -span / 2, -0.65, 0.45, comp);
  leadRun(group, 0.65, span / 2, 0.45, comp);
  drop(group, -span / 2, 0.45, comp);
  drop(group, span / 2, 0.45, comp);
  return group;
}

function buildCurrentSource(comp, span) {
  const group = new THREE.Group();
  // Ring in the X-Y plane with an arrow along the axis (+X, nodes[0]->[1]).
  // NOTE: a smooth torus yields no EdgesGeometry lines at a 30 deg
  // threshold, so the ring itself has no outline; its shaft, arrow head
  // and leads carry the blue edges. That is a rendering limit, not a
  // missing part.
  // Centre height 0.62 = ring radius 0.55 + tube 0.07, so the ring sits
  // on the slab instead of sinking into it.
  const ring = mesh(
    new THREE.TorusGeometry(0.55, 0.07, 16, 48),
    blueprintMaterial(PART_COLORS.currentSource),
    comp,
  );
  ring.position.set(0, 0.62, 0);
  group.add(ring);
  const shaft = xCylinder(0.07, 0.7, blueprintMaterial(PART_COLORS.currentSource), comp);
  shaft.position.set(0, 0.62, 0);
  group.add(shaft);
  const head = xCone(0.16, 0.35, blueprintMaterial(PART_COLORS.currentSource), comp);
  head.position.set(0.45, 0.62, 0);
  group.add(head);
  leadRun(group, -span / 2, -0.62, 0.62, comp);
  leadRun(group, 0.62, span / 2, 0.62, comp);
  drop(group, -span / 2, 0.62, comp);
  drop(group, span / 2, 0.62, comp);
  return group;
}

function buildUnknown(comp, span) {
  const group = new THREE.Group();
  const box = mesh(
    new THREE.BoxGeometry(0.9, 0.9, 0.9),
    blueprintMaterial(BLUEPRINT_FILL),
    comp,
  );
  box.position.set(0, 0.45, 0);
  group.add(box);
  leadRun(group, -span / 2, -0.45, 0.45, comp);
  leadRun(group, 0.45, span / 2, 0.45, comp);
  drop(group, -span / 2, 0.45, comp);
  drop(group, span / 2, 0.45, comp);
  return group;
}

const BUILDERS = Object.freeze({
  resistor: buildResistor,
  capacitor: buildCapacitor,
  led: (comp, span) => buildDiode(comp, span, true),
  diode: (comp, span) => buildDiode(comp, span, false),
  voltage_source: buildVoltageSource,
  current_source: buildCurrentSource,
});

export function buildComponentMesh(comp, span) {
  const build = BUILDERS[comp.type] || buildUnknown;
  const group = build(comp, span);
  return finalizeGroup(group, comp);
}

// ---------------------------------------------------------------------------
// Circuit assembly
// ---------------------------------------------------------------------------

// Mesh-only bounding box of a group: sprites (glow) and lines are
// excluded, so a glow sprite can never inflate a slab, a dimension or
// a camera fit. (Verified: Box3.setFromObject includes sprite children
// even when hidden; see finalizeGroup.)
function meshOnlyBox(root) {
  const box = new THREE.Box3();
  root.updateMatrixWorld(true, true);
  root.traverse((obj) => {
    if (!obj.isMesh) return;
    const b = new THREE.Box3().setFromObject(obj);
    if (!b.isEmpty()) box.union(b);
  });
  return box;
}

function addNodeMarkers(group, nodePositions, ground) {
  for (const [name, pos] of nodePositions) {
    // Node dot: flat disc on top of the wire ribbon.
    const dot = new THREE.Mesh(
      new THREE.CylinderGeometry(0.09, 0.09, 0.03, 24),
      blueprintMaterial(PART_COLORS.nodeDot),
    );
    dot.position.set(pos.x, 0.035, pos.z);
    dot.userData = { kind: 'node', node: name };
    group.add(dot);
    if (name === ground) {
      // Ground glyph: three stacked bars on the slab, in front of the
      // node (toward +Z), recoloured blueprint blue.
      [0.9, 0.6, 0.3].forEach((width, i) => {
        const bar = new THREE.Mesh(
          new THREE.BoxGeometry(width, 0.02, 0.06),
          blueprintMaterial(PART_COLORS.ground),
        );
        bar.position.set(pos.x, 0.03, pos.z + 0.35 + i * 0.15);
        bar.userData = { kind: 'ground', node: name };
        group.add(bar);
      });
    }
  }
}

// One orthogonal polyline per NET (unordered node pair carrying at least
// one component), not per component: parts that share a node share a
// wire. Manhattan routing on the slab top (y = 0.02): straight when the
// nodes share x or z, else a single elbow at (x2, z1). No diagonals.
// Each segment is a flat ribbon (BoxGeometry, 0.12 wide) — a THREE.Line
// would render 1 px and disappear at blueprint zoom — with a square
// joint at each elbow. Ribbons sit at y = 0.02; node dots sit on top.
function routePoints(a, b) {
  const y = 0.02;
  const A = new THREE.Vector3(a.x, y, a.z);
  const B = new THREE.Vector3(b.x, y, b.z);
  if (Math.abs(a.x - b.x) < 1e-9 || Math.abs(a.z - b.z) < 1e-9) return [A, B];
  return [A, new THREE.Vector3(b.x, y, a.z), B];
}

function buildWireGroup(points, wireMaterial) {
  const group = new THREE.Group();
  for (let i = 0; i < points.length - 1; i++) {
    const p = points[i];
    const q = points[i + 1];
    const dx = q.x - p.x;
    const dz = q.z - p.z;
    const len = Math.hypot(dx, dz);
    if (len < 1e-9) continue;
    const seg = new THREE.Mesh(
      new THREE.BoxGeometry(Math.abs(dx) > 1e-9 ? len : 0.12, 0.02, Math.abs(dz) > 1e-9 ? len : 0.12),
      wireMaterial,
    );
    seg.position.set((p.x + q.x) / 2, 0.02, (p.z + q.z) / 2);
    seg.userData = { kind: 'wire-ribbon' };
    group.add(seg);
  }
  // Solid square joint at each elbow so the corner is not notched.
  for (let i = 1; i < points.length - 1; i++) {
    const joint = new THREE.Mesh(
      new THREE.BoxGeometry(0.12, 0.02, 0.12),
      wireMaterial,
    );
    joint.position.copy(points[i]);
    joint.userData = { kind: 'wire-elbow' };
    group.add(joint);
  }
  return group;
}

// Build all net wires. Returns { group, wiresById }: wiresById maps
// component id -> polyline points (with the elbow) for overlay
// setWires, so flow dots travel the orthogonal route.
export function buildWires(nodePositions, components, routes = null) {
  const group = new THREE.Group();
  group.name = 'netproof-wires';
  let wireIndex = 0;
  const groundName = [...nodePositions.keys()].find((n) => n === '0' || n.toLowerCase() === 'gnd');
  const nets = new Map(); // "a\0b" (sorted) -> { a, b, components: [] }
  for (const comp of components) {
    const [a, b] = [...comp.nodes].sort();
    const key = a + '\0' + b;
    if (!nets.has(key)) nets.set(key, { a, b, components: [] });
    nets.get(key).components.push(comp.id);
  }
  const wiresById = {};
  for (const net of nets.values()) {
    const pA = nodePositions.get(net.a);
    const pB = nodePositions.get(net.b);
    if (!pA || !pB) continue;
    // Deterministic elbow order: netlist order (Map insertion order).
    const custom = routes && routes.get(net.components[0]);
    const points = custom
      ? custom.map((p) => new THREE.Vector3(p.x, 0.02, p.z))
      : routePoints(pA, pB);
    const isGroundNet = net.a === groundName || net.b === groundName;
    const wireMaterial = blueprintMaterial(
      isGroundNet ? WIRE_GROUND : WIRE_COLORS[wireIndex++ % WIRE_COLORS.length],
    );
    const wireGroup = buildWireGroup(points, wireMaterial);
    // Drag hook (interaction.js refreshWires): dragging a part moves
    // the part only; the wire re-routes to the part's new foot position.
    // Node positions in the netlist do not change on drag (layout-only),
    // so the hook shifts this net's endpoints by the drag delta.
    const state = {
      nodeA: new THREE.Vector3(pA.x, 0.02, pA.z),
      nodeB: new THREE.Vector3(pB.x, 0.02, pB.z),
    };
    const rebuild = () => {
      for (const child of [...wireGroup.children]) {
        wireGroup.remove(child);
        child.geometry?.dispose?.();
      }
      const fresh = buildWireGroup(routePoints(state.nodeA, state.nodeB), wireMaterial);
      for (const child of [...fresh.children]) wireGroup.add(child);
    };
    wireGroup.userData = {
      wire: true,
      wireFor: net.components[0],
      components: [...net.components],
      endpoints: net.components.map((id) => ({ componentId: id, offset: { x: 0, y: 0, z: 0 } })),
      update: (_id, _newPos, delta) => {
        if (!delta) return;
        state.nodeA.x += delta.x || 0;
        state.nodeA.z += delta.z || 0;
        state.nodeB.x += delta.x || 0;
        state.nodeB.z += delta.z || 0;
        rebuild();
      },
    };
    group.add(wireGroup);
    // Flow-dot points ride just above the ribbon so the dots sit on it.
    const flowPoints = points.map((p) => new THREE.Vector3(p.x, 0.06, p.z));
    for (const id of net.components) wiresById[id] = flowPoints.map((p) => p.clone());
  }
  return { group, wiresById };
}

// Raised slab: RoundedBoxGeometry sized from the circuit bounding box
// (width/depth + 1.6, height 0.18, radius 0.12), top face exactly y = 0.
// NOTE: EdgesGeometry on a RoundedBoxGeometry yields zero edges at a
// 30 deg threshold (verified against three@0.160.0 — the rounded
// corners have no crisp rim), so the slab outline is a rounded-rect
// LineLoop traced on the top face instead of EdgesGeometry. Same blue,
// same read, no noisy facet lines.
export function buildSlab(box) {
  const safe = box && !box.isEmpty()
    ? box
    : new THREE.Box3(new THREE.Vector3(-1.5, 0, -1.5), new THREE.Vector3(1.5, 0, 1.5));
  const width = safe.max.x - safe.min.x + SLAB_PAD;
  const depth = safe.max.z - safe.min.z + SLAB_PAD;
  const cx = (safe.min.x + safe.max.x) / 2;
  const cz = (safe.min.z + safe.max.z) / 2;
  const group = new THREE.Group();
  group.name = 'netproof-slab';
  const slab = new THREE.Mesh(
    new RoundedBoxGeometry(width, SLAB_HEIGHT, depth, 4, SLAB_RADIUS),
    blueprintMaterial(PART_COLORS.mat, { roughness: 0.85 }),
  );
  slab.receiveShadow = true;
  slab.position.set(cx, -SLAB_HEIGHT / 2, cz);
  slab.userData = { kind: 'slab' };
  group.add(slab);
  // Rounded-rect outline loop on the top face (y = 0.01, just above it).
  const r = Math.min(SLAB_RADIUS, width / 2, depth / 2);
  const hw = width / 2;
  const hd = depth / 2;
  const pts = [];
  const corners = [
    [cx + hw - r, cz + hd - r, 0],
    [cx - hw + r, cz + hd - r, Math.PI / 2],
    [cx - hw + r, cz - hd + r, Math.PI],
    [cx + hw - r, cz - hd + r, (3 * Math.PI) / 2],
  ];
  const SEG = 6;
  for (const [ccx, ccz, start] of corners) {
    for (let i = 0; i <= SEG; i++) {
      const a = start + (i / SEG) * (Math.PI / 2);
      pts.push(new THREE.Vector3(ccx + r * Math.cos(a), 0.01, ccz + r * Math.sin(a)));
    }
  }
  const outline = new THREE.LineLoop(
    new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineBasicMaterial({ color: PART_COLORS.matRim }),
  );
  outline.raycast = () => {};
  outline.userData = { kind: 'slab-outline' };
  // Cutting-mat grid on the mat top (0.5-unit squares), faint white.
  const step = 0.5;
  const gridPts = [];
  for (let x = cx - hw + step; x < cx + hw - 1e-6; x += step) {
    gridPts.push(x, 0.004, cz - hd, x, 0.004, cz + hd);
  }
  for (let z = cz - hd + step; z < cz + hd - 1e-6; z += step) {
    gridPts.push(cx - hw, 0.004, z, cx + hw, 0.004, z);
  }
  const gridGeo = new THREE.BufferGeometry();
  gridGeo.setAttribute('position', new THREE.Float32BufferAttribute(gridPts, 3));
  const matGrid = new THREE.LineSegments(
    gridGeo,
    new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.16 }),
  );
  matGrid.raycast = () => {};
  matGrid.userData = { kind: 'slab-grid' };
  group.add(matGrid);
  group.add(outline);
  group.userData = {
    kind: 'slab',
    width,
    depth,
    centreX: cx,
    centreZ: cz,
  };
  return { group, width, depth, centreX: cx, centreZ: cz };
}

// Dimension lines: one along the slab's front X edge, one along its
// right Z edge, each offset 0.5 outside the slab at y = 0.02 — thin blue
// line with 0.18 end ticks and extension lines from the slab corners.
// Values are the live slab size in scene units to one decimal, no unit
// suffix (scene units are not millimetres). A zero-size axis gets no line.
export function buildDimensionLines(slab) {
  const group = new THREE.Group();
  group.name = 'netproof-dimensions';
  const positions = [];
  const dimensions = [];
  const { width, depth, centreX, centreZ } = slab;
  const y = 0.02;
  const push = (ax, ay, az, bx, by, bz) => {
    positions.push(ax, ay, az, bx, by, bz);
  };
  if (width > 0.01) {
    const zDim = centreZ + depth / 2 + 0.5;
    const x0 = centreX - width / 2;
    const x1 = centreX + width / 2;
    push(x0, y, zDim, x1, y, zDim); // dimension line
    push(x0, y, zDim - 0.09, x0, y, zDim + 0.09); // end ticks
    push(x1, y, zDim - 0.09, x1, y, zDim + 0.09);
    push(x0, y, centreZ + depth / 2, x0, y, zDim); // extension lines
    push(x1, y, centreZ + depth / 2, x1, y, zDim);
    dimensions.push({ axis: 'x', value: Number(width.toFixed(1)) });
  }
  if (depth > 0.01) {
    const xDim = centreX + width / 2 + 0.5;
    const z0 = centreZ - depth / 2;
    const z1 = centreZ + depth / 2;
    push(xDim, y, z0, xDim, y, z1); // dimension line
    push(xDim - 0.09, y, z0, xDim + 0.09, y, z0); // end ticks
    push(xDim - 0.09, y, z1, xDim + 0.09, y, z1);
    push(centreX + width / 2, y, z0, xDim, y, z0); // extension lines
    push(centreX + width / 2, y, z1, xDim, y, z1);
    dimensions.push({ axis: 'z', value: Number(depth.toFixed(1)) });
  }
  if (positions.length) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    const lines = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: PART_COLORS.dimension, transparent: true, opacity: 0.7 }));
    lines.raycast = () => {};
    lines.userData = { kind: 'dimension-lines' };
    group.add(lines);
  }
  group.userData = { kind: 'dimensions', dimensions };
  return { group, dimensions };
}

export function buildCircuit(netlist) {
  const components = normalizeNetlist(netlist);
  // Ground-plane remap of the layout grid (see header note): layout
  // topology is untouched, only the rendering frame changes.
  const loop = loopLayout(components);
  const routes = loop ? loop.routes : null;
  const nodePositions = loop
    ? loop.positions
    : new Map(
      [...layoutNodes(components).entries()].map(([name, p]) => [name, new THREE.Vector3(p.x, 0, p.y)]),
    );
  const group = new THREE.Group();
  group.name = 'netproof-circuit';

  const ground =
    [...nodePositions.keys()].find((n) => n === GROUND_NODE) ||
    [...nodePositions.keys()].find((n) => n.toLowerCase() === 'gnd') ||
    null;
  addNodeMarkers(group, nodePositions, ground);

  const wires = buildWires(nodePositions, components, routes);
  group.add(wires.group);

  const meshesById = new Map();
  const xAxis = new THREE.Vector3(1, 0, 0);
  components.forEach((comp, index) => {
    const route = routes && routes.get(comp.id);
    const seg = route ? longestSegment(route) : null;
    const p1 = seg ? seg.a : nodePositions.get(comp.nodes[0]);
    const p2 = seg ? seg.b : nodePositions.get(comp.nodes[1]);
    const dir = new THREE.Vector3().subVectors(p2, p1);
    const span = dir.length();
    const mid = new THREE.Vector3().addVectors(p1, p2).multiplyScalar(0.5);
    const compGroup = buildComponentMesh(comp, span);
    compGroup.position.copy(mid);
    compGroup.quaternion.setFromUnitVectors(xAxis, dir.clone().normalize());
    compGroup.userData.baseY = mid.y;
    compGroup.userData.explodeIndex = index;
    group.add(compGroup);
    meshesById.set(comp.id, compGroup);
  });

  // Slab + dimensions from the mesh-only bounding box (glow sprites
  // excluded, so they can never inflate the sheet).
  const box = meshOnlyBox(group);
  const slab = buildSlab(box);
  group.add(slab.group);
  const dims = buildDimensionLines(slab);
  group.add(dims.group);

  return {
    group,
    nodePositions,
    meshesById,
    slab: slab.group,
    slabSize: { width: slab.width, height: SLAB_HEIGHT, depth: slab.depth },
    dimensions: dims.dimensions,
    wiresById: wires.wiresById,
    // Alias: the elbow-following polyline points per component id, for
    // overlay setWires (flow dots travel the orthogonal route).
    wirePointsById: wires.wiresById,
  };
}
