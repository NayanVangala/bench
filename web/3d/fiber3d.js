// Fiber link on the bench: the same shape main3d expects from buildCircuit
// ({ group, meshesById, wirePointsById }), so picking, callouts, severity
// tints, framing and Inspect all work unchanged.
//
// Layout: one slot per element along X. A thin pigtail runs through the
// whole chain; fiber elements are a thicker jacket over their slot (with a
// coil when long). Colours follow industry codes: single-mode jacket
// yellow, multimode aqua; UPC connector boots blue, APC green.

import * as THREE from 'three';
import { blueprintMaterial, buildSlab } from './components.js';

const SLOT = 1.5;
const AXIS_Y = 0.32;
const JACKET = { smf: 0xf2c94c, mmf: 0x2ec4c6 };
const BOOT = { APC: 0x2fa84f, UPC: 0x2f6fde, PC: 0x2f6fde };

function tag(obj, el) {
  obj.traverse((o) => {
    if (o.isMesh) {
      o.userData = { componentId: el.id, type: el.type };
      o.castShadow = true;
      o.receiveShadow = true;
    }
  });
  obj.userData = { componentId: el.id, type: el.type, baseY: 0 };
  return obj;
}

function box(w, h, d, color) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), blueprintMaterial(color));
  m.position.y = h / 2;
  return m;
}

function xCyl(r, len, color, extra) {
  const g = new THREE.CylinderGeometry(r, r, len, 24);
  g.rotateZ(Math.PI / 2);
  const m = new THREE.Mesh(g, blueprintMaterial(color, extra));
  m.position.y = AXIS_Y;
  return m;
}

// Wavelength -> a visible tint for the laser's band stripe (IR shows as
// deep red / violet, since it is invisible in reality).
function bandColor(nm) {
  if (nm >= 1260) return 0x9b2c8a;
  if (nm >= 780) return 0xb3261e;
  if (nm >= 620) return 0xe5484d;
  if (nm >= 570) return 0xf5c542;
  if (nm >= 495) return 0x34c26a;
  return 0x3b82f6;
}

function buildElement(el, link) {
  const g = new THREE.Group();
  switch (el.type) {
    case 'laser': {
      g.add(box(1.1, 0.7, 0.8, 0x2a2d33));
      const stripe = box(1.12, 0.12, 0.82, bandColor(el.wavelength_nm || 1550));
      stripe.position.y = 0.55;
      g.add(stripe);
      const port = xCyl(0.1, 0.25, 0xb9bec7, { metalness: 0.6, roughness: 0.35 });
      port.position.x = 0.62;
      g.add(port);
      break;
    }
    case 'detector': {
      g.add(box(0.9, 0.7, 0.8, 0x1d2026));
      const lens = xCyl(0.16, 0.1, 0x8db8ff, { roughness: 0.15 });
      lens.position.x = -0.48;
      g.add(lens);
      break;
    }
    case 'connector': {
      g.add(xCyl(0.11, 0.42, 0xb9bec7, { metalness: 0.6, roughness: 0.35 }));
      for (const [side, polish] of [[-1, el.polish_a], [1, el.polish_b]]) {
        const boot = xCyl(0.08, 0.28, BOOT[polish || 'UPC']);
        boot.position.x = side * 0.34;
        g.add(boot);
      }
      break;
    }
    case 'splice':
      g.add(xCyl(0.06, 0.6, 0xe9eef5, { transparent: true, opacity: 0.85 }));
      break;
    case 'splitter': {
      g.add(box(0.8, 0.22, 0.55, 0xf4f6fa));
      const n = Math.min(el.ports || 2, 4);
      for (let i = 0; i < n; i++) {
        const z = (i - (n - 1) / 2) * 0.13;
        const out = xCyl(0.025, 0.4, JACKET.smf);
        out.position.set(0.55, 0.11, z);
        g.add(out);
      }
      break;
    }
    case 'attenuator':
      g.add(xCyl(0.15, 0.6, 0xf29a2e));
      break;
    case 'isolator':
      g.add(xCyl(0.2, 0.8, 0xc9a14a, { metalness: 0.5, roughness: 0.4 }));
      break;
    case 'fiber': {
      const kind = el.fiber_type || 'smf';
      g.add(xCyl(0.06, SLOT * 0.9, JACKET[kind] ?? JACKET.smf));
      if ((el.length_m || 1) > 5) {
        // Long run: a spool of loops standing on the mat.
        for (let i = 0; i < 4; i++) {
          const loop = new THREE.Mesh(
            new THREE.TorusGeometry(0.42, 0.035, 10, 40),
            blueprintMaterial(JACKET[kind] ?? JACKET.smf),
          );
          loop.position.set(0, 0.48, -0.55 + i * 0.05);
          g.add(loop);
        }
      }
      break;
    }
    default:
      g.add(box(0.5, 0.5, 0.5, 0xc9ced6));
  }
  return tag(g, el);
}

export function buildFiberLink(link, report) {
  const elements = (link && Array.isArray(link.elements)) ? link.elements : [];
  const group = new THREE.Group();
  group.name = 'netproof-fiber-link';
  const meshesById = new Map();
  const x0 = -((elements.length - 1) * SLOT) / 2;

  // Pigtail through the whole chain (continuity between bodies).
  if (elements.length > 1) {
    const len = (elements.length - 1) * SLOT;
    const pig = xCyl(0.03, len, JACKET.smf);
    pig.raycast = () => {};
    group.add(pig);
  }
  elements.forEach((el, i) => {
    const obj = buildElement(el, link);
    obj.position.x = x0 + i * SLOT;
    obj.userData.explodeIndex = i;
    group.add(obj);
    meshesById.set(el.id, obj);
  });

  const bbox = new THREE.Box3().setFromObject(group);
  if (!bbox.isEmpty()) {
    const slab = buildSlab(bbox);
    group.add(slab.group);
  }
  // Light pulses: travel laser -> detector along the chain, dimming with the
  // power the budget computed after each element (report.trace, dBm).
  const trace = (report && report.trace) || [];
  const launch = report ? Math.pow(10, (report.launch_dbm || 0) / 10) : 1;
  const powerAt = (x) => {
    const slot = Math.round((x - x0) / SLOT);
    const row = trace[Math.max(0, Math.min(trace.length - 1, slot))];
    return row ? row.power_mw / launch : 1;
  };
  const laser = elements.find((e) => e.type === 'laser');
  const color = bandColor((laser && laser.wavelength_nm) || 1550);
  const pulses = [];
  const span = Math.max(1, (elements.length - 1) * SLOT);
  if (elements.length > 1 && report) {
    for (let k = 0; k < 7; k++) {
      const p = new THREE.Mesh(new THREE.SphereGeometry(0.09, 10, 8),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 1, blending: THREE.AdditiveBlending, depthWrite: false }));
      p.raycast = () => {};
      p.userData = { kind: 'pulse', u: k / 7 };
      group.add(p);
      pulses.push(p);
    }
  }
  function animate(dt) {
    for (const p of pulses) {
      p.userData.u = (p.userData.u + dt * 0.18) % 1;
      const x = x0 + p.userData.u * span;
      p.position.set(x, AXIS_Y, 0);
      const f = Math.max(0.04, Math.min(1, powerAt(x)));
      p.material.opacity = 0.25 + 0.75 * Math.sqrt(f);
      p.scale.setScalar(0.6 + 0.8 * Math.sqrt(f));
    }
  }
  return { group, meshesById, wirePointsById: {}, nodePositions: new Map(), animate };
}
