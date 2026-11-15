// Free-space optics bench on the shared bench: same contract as
// buildCircuit / buildFiberLink ({ group, meshesById, wirePointsById }).
//
// Scale: 1 scene unit = one 25 mm breadboard hole pitch. Table x_mm maps to
// scene x, table y_mm to scene -z (so +y points away from the viewer, like a
// top-view drawing). Beams come only from the server's ray-trace segments.

import * as THREE from 'three';
import { blueprintMaterial } from './components.js';

const PITCH = 25;
const PLATE_T = 0.5;
const BEAM_H = 3.0; // beam height above the plate (75 mm posts)

function wavelengthColor(nm) {
  if (nm >= 700) return 0xd0332e;
  if (nm >= 620) return 0xff3b2f;
  if (nm >= 590) return 0xff9f1a;
  if (nm >= 570) return 0xffe23a;
  if (nm >= 495) return 0x3cff6e;
  if (nm >= 450) return 0x3b8bff;
  return 0x9b5cff;
}

function dirVec(deg) {
  const r = (deg * Math.PI) / 180;
  return new THREE.Vector3(Math.cos(r), 0, -Math.sin(r));
}

function axisMesh(geo, mat, dir) {
  // Cylinder geometry is Y-aligned: turn its axis onto `dir`.
  const m = new THREE.Mesh(geo, mat);
  m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.clone().normalize());
  return m;
}

function post(group) {
  const metal = blueprintMaterial(0xb9bec7, { metalness: 0.6, roughness: 0.35 });
  const p = new THREE.Mesh(new THREE.CylinderGeometry(0.25, 0.25, BEAM_H - 0.4, 16), metal);
  p.position.y = (BEAM_H - 0.4) / 2;
  const base = new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.15, 0.9), metal);
  base.position.y = 0.075;
  group.add(p, base);
}

function buildElement(e, wl) {
  const g = new THREE.Group();
  const d = dirVec(e.angle_deg || 0);
  post(g);
  const atBeam = (m) => { m.position.y = BEAM_H; g.add(m); return m; };
  switch (e.type) {
    case 'laser': {
      const body = axisMesh(new THREE.CylinderGeometry(0.45, 0.45, 2.2, 24), blueprintMaterial(0x2a2d33), d);
      atBeam(body).position.addScaledVector(d, -0.9);
      const tip = axisMesh(new THREE.CylinderGeometry(0.2, 0.2, 0.3, 16),
        blueprintMaterial(wavelengthColor(wl), { emissive: wavelengthColor(wl), emissiveIntensity: 0.6 }), d);
      atBeam(tip).position.addScaledVector(d, 0.3);
      break;
    }
    case 'mirror': {
      atBeam(axisMesh(new THREE.CylinderGeometry(0.5, 0.5, 0.12, 32), blueprintMaterial(0xdfe7f2, { metalness: 0.9, roughness: 0.1 }), d));
      const mount = axisMesh(new THREE.BoxGeometry(1.2, 0.3, 1.2), blueprintMaterial(0x23262d), d);
      atBeam(mount).position.addScaledVector(d, -0.2);
      break;
    }
    case 'beamsplitter': {
      const cube = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5),
        blueprintMaterial(0xbfe3ff, { transparent: true, opacity: 0.55, roughness: 0.05 }));
      cube.rotation.y = ((e.angle_deg || 0) + 45) * Math.PI / 180;
      atBeam(cube);
      break;
    }
    case 'lens':
      atBeam(axisMesh(new THREE.CylinderGeometry(0.5, 0.5, 0.1, 32), blueprintMaterial(0xcfe8ff, { transparent: true, opacity: 0.55, roughness: 0.05 }), d));
      atBeam(axisMesh(new THREE.TorusGeometry(0.55, 0.06, 8, 32).rotateX(Math.PI / 2), blueprintMaterial(0x23262d), d));
      break;
    case 'filter':
      atBeam(axisMesh(new THREE.CylinderGeometry(0.5, 0.5, 0.1, 32), blueprintMaterial(0x3a3d44, { transparent: true, opacity: 0.8 }), d));
      atBeam(axisMesh(new THREE.TorusGeometry(0.55, 0.06, 8, 32).rotateX(Math.PI / 2), blueprintMaterial(0x23262d), d));
      break;
    case 'dump':
      atBeam(axisMesh(new THREE.BoxGeometry(1.0, 0.4, 1.0), blueprintMaterial(0x15171b), d));
      break;
    case 'detector': {
      const body = new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.9, 0.9), blueprintMaterial(0x1d2026));
      atBeam(body).position.addScaledVector(d, 0.4);
      atBeam(axisMesh(new THREE.CylinderGeometry(0.2, 0.2, 0.06, 24), blueprintMaterial(0x6a8cff), d)).position.addScaledVector(d, -0.07);
      break;
    }
    default:
      atBeam(new THREE.Mesh(new THREE.BoxGeometry(0.6, 0.6, 0.6), blueprintMaterial(0xc9ced6)));
  }
  g.traverse((o) => {
    if (o.isMesh) {
      o.userData = { componentId: e.id, type: e.type };
      o.castShadow = true;
      o.receiveShadow = true;
    }
  });
  g.userData = { componentId: e.id, type: e.type, baseY: 0 };
  return g;
}

export function buildOpticsBench(bench, report) {
  const group = new THREE.Group();
  group.name = 'netproof-optics-bench';
  const meshesById = new Map();
  const W = (bench && bench.width_mm) || 600;
  const D = (bench && bench.depth_mm) || 300;
  const toScene = (x, y) => new THREE.Vector3((x - W / 2) / PITCH, PLATE_T, -(y - D / 2) / PITCH);

  // Breadboard: black anodized plate with the 25 mm hole grid.
  const plate = new THREE.Mesh(new THREE.BoxGeometry(W / PITCH + 1, PLATE_T, D / PITCH + 1), blueprintMaterial(0x2b2d33, { roughness: 0.8 }));
  plate.position.y = PLATE_T / 2;
  plate.receiveShadow = true;
  plate.raycast = () => {};
  group.add(plate);
  const cols = Math.floor(W / PITCH) + 1;
  const rows = Math.floor(D / PITCH) + 1;
  const holes = new THREE.InstancedMesh(new THREE.CylinderGeometry(0.12, 0.12, 0.02, 10), new THREE.MeshBasicMaterial({ color: 0x0c0d10 }), cols * rows);
  const m = new THREE.Matrix4();
  let k = 0;
  for (let i = 0; i < cols; i++) {
    for (let j = 0; j < rows; j++) {
      const p = toScene(i * PITCH, j * PITCH);
      m.makeTranslation(p.x, PLATE_T + 0.011, p.z);
      holes.setMatrixAt(k++, m);
    }
  }
  holes.raycast = () => {};
  group.add(holes);

  const lasers = (bench.elements || []).filter((e) => e.type === 'laser');
  const wl = (lasers[0] && lasers[0].wavelength_nm) || 633;
  (bench.elements || []).forEach((e, i) => {
    const obj = buildElement(e, wl);
    obj.position.copy(toScene(e.x_mm, e.y_mm));
    obj.userData.explodeIndex = i;
    group.add(obj);
    meshesById.set(e.id, obj);
  });

  // Beams: one glowing rod per traced segment; thicker and brighter with power.
  const segs = (report && report.segments) || [];
  const maxP = Math.max(1e-9, ...segs.map((s) => s.power_mw));
  const color = wavelengthColor(wl);
  for (const s of segs) {
    const a = toScene(s.from[0], s.from[1]).setY(PLATE_T + BEAM_H);
    const b = toScene(s.to[0], s.to[1]).setY(PLATE_T + BEAM_H);
    const len = a.distanceTo(b);
    if (len < 1e-6) continue;
    const frac = Math.max(0.15, Math.sqrt(s.power_mw / maxP));
    const rod = axisMesh(
      new THREE.CylinderGeometry(0.035 + 0.04 * frac, 0.035 + 0.04 * frac, len, 8),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.35 + 0.6 * frac }),
      b.clone().sub(a),
    );
    rod.position.copy(a).add(b).multiplyScalar(0.5);
    rod.raycast = () => {};
    rod.userData = { kind: 'beam' };
    group.add(rod);
  }
  return { group, meshesById, wirePointsById: {}, nodePositions: new Map() };
}
