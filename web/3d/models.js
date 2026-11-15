// Real part models from the KiCad 3D library (web/3d/models/kicad/,
// CC-BY-SA 4.0, see README.md there). Loaded once at startup; until they
// arrive (or if they fail to load) components.js falls back to its
// procedural shapes, so the bench always renders.
//
// Normalisation: KiCad models are Z-up, in 2.54 mm VRML units, with the
// origin on pin 1 and the pins along +X. Each template is rotated to Y-up,
// scaled to scene units (MODEL_MM per mm), centred on X/Z with its lowest
// point on the mat (y = 0), so its two pins sit at x = +-pinHalf.
// Polarised parts are turned so pin 1 (cathode for LED / diode) is at +X:
// components.js puts nodes[0] (anode) at -X.

import * as THREE from 'three';
import { VRMLLoader } from 'three/addons/loaders/VRMLLoader.js';

export const MODEL_MM = 0.24; // scene units per millimetre
const WRL_UNIT_MM = 2.54;

const SPECS = {
  resistor: { file: 'resistor_axial.wrl', pitchMm: 10.16, flip: false }, // DIN0207, L 6.3 D 2.5
  // LED drawn at 70 % so the 5 mm dome doesn't hide its neighbours on the bench.
  led: { file: 'led_5mm.wrl', pitchMm: 2.54, flip: true, scale: 0.7 },
  capacitor: { file: 'cap_radial.wrl', pitchMm: 2.0, flip: false },
  diode: { file: 'diode_do41.wrl', pitchMm: 10.16, flip: true },
};

const templates = new Map(); // type -> { object, pinHalf, size }

function normalize(root, spec) {
  const outer = new THREE.Group();
  const turn = new THREE.Group();
  root.rotation.x = -Math.PI / 2;
  root.scale.setScalar(WRL_UNIT_MM * MODEL_MM * (spec.scale || 1));
  turn.add(root);
  if (spec.flip) turn.rotation.y = Math.PI;
  outer.add(turn);
  outer.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(outer);
  const c = box.getCenter(new THREE.Vector3());
  turn.position.set(-c.x, -box.min.y, -c.z);
  outer.updateMatrixWorld(true);
  const size = new THREE.Box3().setFromObject(outer).getSize(new THREE.Vector3());
  return { object: outer, pinHalf: (spec.pitchMm * MODEL_MM * (spec.scale || 1)) / 2, size };
}

async function loadAll() {
  const loader = new VRMLLoader();
  await Promise.all(Object.entries(SPECS).map(async ([type, spec]) => {
    try {
      const url = new URL(`./models/kicad/${spec.file}`, import.meta.url).href;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // three's VRML lexer rejects '-' in DEF/USE names (KiCad uses
      // "RES-THT-01"); rename in memory, the shipped file stays unmodified.
      const text = (await res.text()).replace(
        /\b(DEF|USE)\s+([A-Za-z0-9_-]+)/g,
        (_, kw, name) => `${kw} ${name.replace(/-/g, '_')}`,
      );
      const root = loader.parse(text, url.slice(0, url.lastIndexOf('/') + 1));
      templates.set(type, normalize(root, spec));
    } catch (err) {
      // Missing model: that part type keeps its procedural shape.
      if (typeof console !== 'undefined') console.warn('[netproof 3D] model not loaded:', spec.file, err);
    }
  }));
}

/** Resolves once every model has loaded or failed; never rejects. */
export const partModelsReady = loadAll();

/**
 * A fresh copy of the part model for `type` (own materials, so severity /
 * selection tints stay per part), or null when there is no model.
 * Returns { object, pinHalf, size }.
 */
export function partModel(type) {
  const t = templates.get(type);
  if (!t) return null;
  const object = t.object.clone(true);
  object.traverse((o) => {
    if (o.isMesh && o.material) {
      o.material = Array.isArray(o.material) ? o.material.map((m) => m.clone()) : o.material.clone();
    }
  });
  return { object, pinHalf: t.pinHalf, size: t.size.clone() };
}
