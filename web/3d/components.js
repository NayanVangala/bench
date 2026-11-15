// Netproof 3D components — meshes built from a netlist (frontend-only).
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
// Three.js comes from the same CDN + version as web/3d/scene.js
// (unpkg, three@0.160.0) so both modules share one module instance.

import * as THREE from 'https://unpkg.com/three@0.160.0/build/three.module.js';

export const GRID_X = 3; // node column spacing (scene units)
export const GRID_Y = 2; // node row spacing (scene units)
export const GROUND_NODE = '0';

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

// ---------------------------------------------------------------------------
// Mesh helpers. Every builder returns a THREE.Group whose local +X axis
// runs from nodes[0] to nodes[1], centred on the origin, spanning `span`
// (the distance between the two node positions). buildCircuit orients it.
// ---------------------------------------------------------------------------

function stdMaterial(color, opts = {}) {
  return new THREE.MeshStandardMaterial({ color, roughness: 0.55, metalness: 0.15, ...opts });
}

function mesh(geometry, material, comp) {
  const m = new THREE.Mesh(geometry, material);
  m.userData = userDataFor(comp);
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

// Straight wire tube from local x=a to x=b on the component axis.
function wireTube(a, b, comp, radius = 0.06, color = 0xb87333) {
  if (Math.abs(b - a) < 1e-6) return null;
  const curve = new THREE.LineCurve3(new THREE.Vector3(a, 0, 0), new THREE.Vector3(b, 0, 0));
  const tube = new THREE.Mesh(
    new THREE.TubeGeometry(curve, 1, radius, 8, false),
    stdMaterial(color, { metalness: 0.8, roughness: 0.35 }),
  );
  tube.userData = userDataFor(comp);
  return tube;
}

function addWire(group, a, b, comp, radius, color) {
  const w = wireTube(a, b, comp, radius, color);
  if (w) group.add(w);
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

function buildResistor(comp, span) {
  const group = new THREE.Group();
  const bodyHalf = Math.min(0.6, span * 0.25);
  const body = xCylinder(0.32, bodyHalf * 2, stdMaterial(0xd8b98a, { roughness: 0.6 }), comp);
  group.add(body);
  // Colour bands from the resistance value (standard colour code).
  const bandColors = resistorBandColors(comp.value);
  const bandX = [-0.4, -0.15, 0.1, 0.35].map((f) => f * (bodyHalf / 0.6));
  bandColors.forEach((color, i) => {
    const band = xCylinder(0.335, 0.09, stdMaterial(color, { roughness: 0.5 }), comp);
    band.position.x = bandX[i];
    group.add(band);
  });
  addWire(group, -span / 2, -bodyHalf, comp);
  addWire(group, bodyHalf, span / 2, comp);
  return group;
}

function buildCapacitor(comp, span) {
  const group = new THREE.Group();
  // Two parallel plates, faces normal to the component axis, gap between.
  for (const x of [-0.18, 0.18]) {
    const plate = mesh(
      new THREE.BoxGeometry(0.07, 1.3, 1.0),
      stdMaterial(0x9fb4c8, { metalness: 0.6, roughness: 0.35 }),
      comp,
    );
    plate.position.x = x;
    group.add(plate);
  }
  addWire(group, -span / 2, -0.22, comp);
  addWire(group, 0.22, span / 2, comp);
  return group;
}

function buildDiode(comp, span, isLed) {
  const group = new THREE.Group();
  const colorHex = isLed ? ledColorHex(comp.color || 'red') : 0xff8a5c;
  // Triangle (cone) pointing anode -> cathode (+X), cathode bar at +X.
  const cone = xCone(
    0.5,
    0.9,
    stdMaterial(colorHex, {
      emissive: isLed ? colorHex : 0x000000,
      emissiveIntensity: isLed ? 0.9 : 0,
      roughness: 0.4,
    }),
    comp,
  );
  cone.position.x = -0.05;
  group.add(cone);
  const bar = mesh(
    new THREE.BoxGeometry(0.08, 1.1, 1.1),
    stdMaterial(0x333333, { metalness: 0.4, roughness: 0.5 }),
    comp,
  );
  bar.position.x = 0.5;
  group.add(bar);
  if (isLed) {
    // Translucent emissive lens over the triangle: the glow.
    const lens = mesh(
      new THREE.SphereGeometry(0.45, 32, 24),
      new THREE.MeshStandardMaterial({
        color: colorHex,
        emissive: colorHex,
        emissiveIntensity: 1.4,
        transparent: true,
        opacity: 0.9,
        roughness: 0.25,
      }),
      comp,
    );
    group.add(lens);
  }
  addWire(group, -span / 2, -0.5, comp);
  addWire(group, 0.54, span / 2, comp);
  return group;
}

function buildVoltageSource(comp, span) {
  const group = new THREE.Group();
  // Battery cylinder; nodes[0] (+) is at local -X, nodes[1] (-) at +X.
  const body = xCylinder(0.45, 1.0, stdMaterial(0xc0c0c8, { metalness: 0.85, roughness: 0.3 }), comp);
  group.add(body);
  // + terminal nub (copper) and a "+" mark in the Y-Z plane at the + end.
  const nub = xCylinder(0.16, 0.25, stdMaterial(0xb87333, { metalness: 0.85, roughness: 0.3 }), comp);
  nub.position.x = -0.62;
  group.add(nub);
  const plusMat = stdMaterial(0xd43d2a, { roughness: 0.5 });
  const plusV = mesh(new THREE.BoxGeometry(0.05, 0.42, 0.08), plusMat, comp);
  plusV.position.x = -0.78;
  const plusH = mesh(new THREE.BoxGeometry(0.05, 0.08, 0.42), plusMat, comp);
  plusH.position.x = -0.78;
  group.add(plusV, plusH);
  // - terminal nub (dark) at the far end.
  const neg = xCylinder(0.16, 0.12, stdMaterial(0x333333, { metalness: 0.5, roughness: 0.5 }), comp);
  neg.position.x = 0.56;
  group.add(neg);
  addWire(group, -span / 2, -0.75, comp);
  addWire(group, 0.62, span / 2, comp);
  return group;
}

function buildCurrentSource(comp, span) {
  const group = new THREE.Group();
  // Ring in the X-Y plane with an arrow along the axis (+X, nodes[0]->[1]).
  const ring = mesh(
    new THREE.TorusGeometry(0.55, 0.07, 16, 48),
    stdMaterial(0xb388ff, { metalness: 0.3, roughness: 0.4 }),
    comp,
  );
  group.add(ring);
  const shaft = xCylinder(0.07, 0.7, stdMaterial(0xb388ff, { metalness: 0.3, roughness: 0.4 }), comp);
  group.add(shaft);
  const head = xCone(0.16, 0.35, stdMaterial(0xb388ff, { metalness: 0.3, roughness: 0.4 }), comp);
  head.position.x = 0.45;
  group.add(head);
  addWire(group, -span / 2, -0.62, comp);
  addWire(group, 0.62, span / 2, comp);
  return group;
}

function buildUnknown(comp, span) {
  const group = new THREE.Group();
  const box = mesh(
    new THREE.BoxGeometry(0.9, 0.9, 0.9),
    stdMaterial(0x8b949e, { roughness: 0.6 }),
    comp,
  );
  group.add(box);
  addWire(group, -span / 2, -0.45, comp);
  addWire(group, 0.45, span / 2, comp);
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
  tagGroup(group, comp);
  return group;
}

// ---------------------------------------------------------------------------
// Circuit assembly
// ---------------------------------------------------------------------------

function addNodeMarkers(group, nodePositions, ground) {
  const markerMat = stdMaterial(0x333333, { metalness: 0.4, roughness: 0.5 });
  for (const [name, pos] of nodePositions) {
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.12, 16, 12), markerMat);
    dot.position.copy(pos);
    dot.userData = { kind: 'node', node: name };
    group.add(dot);
    if (name === ground) {
      // Ground glyph: three stacked bars under the node.
      [0.9, 0.6, 0.3].forEach((width, i) => {
        const bar = new THREE.Mesh(
          new THREE.BoxGeometry(width, 0.06, 0.06),
          stdMaterial(0x333333, { metalness: 0.4, roughness: 0.5 }),
        );
        bar.position.set(pos.x, pos.y - 0.35 - i * 0.15, pos.z);
        bar.userData = { kind: 'ground', node: name };
        group.add(bar);
      });
    }
  }
}

export function buildCircuit(netlist) {
  const components = normalizeNetlist(netlist);
  const nodePositions = layoutNodes(components);
  const group = new THREE.Group();
  group.name = 'netproof-circuit';

  const ground =
    [...nodePositions.keys()].find((n) => n === GROUND_NODE) ||
    [...nodePositions.keys()].find((n) => n.toLowerCase() === 'gnd') ||
    null;
  addNodeMarkers(group, nodePositions, ground);

  const meshesById = new Map();
  const xAxis = new THREE.Vector3(1, 0, 0);
  for (const comp of components) {
    const p1 = nodePositions.get(comp.nodes[0]);
    const p2 = nodePositions.get(comp.nodes[1]);
    const dir = new THREE.Vector3().subVectors(p2, p1);
    const span = dir.length();
    const mid = new THREE.Vector3().addVectors(p1, p2).multiplyScalar(0.5);
    const compGroup = buildComponentMesh(comp, span);
    compGroup.position.copy(mid);
    compGroup.quaternion.setFromUnitVectors(xAxis, dir.clone().normalize());
    group.add(compGroup);
    meshesById.set(comp.id, compGroup);
  }

  return { group, nodePositions, meshesById };
}
