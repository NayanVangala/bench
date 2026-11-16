// Live circuit physics on the bench, driven only by the latest check report:
// LEDs light up in their own colour with brightness from their simulated
// current, and resistors glow hot as their dissipated power nears the
// rating. Visual only; never changes a design or a verdict.

import * as THREE from 'three';
import { ledColorHex } from './components.js';

const LED_FULL_A = 0.02;      // 20 mA reads as "fully lit"
const HEAT_START = 0.15;      // resistors start to glow at 15 % of rating
const HEAT_COLOR = 0xff5a1f;

let glowTex = null;
function glowTexture() {
  if (glowTex) return glowTex;
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, 'rgba(255,255,255,1)');
  grad.addColorStop(0.35, 'rgba(255,255,255,0.45)');
  grad.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  glowTex = new THREE.CanvasTexture(c);
  return glowTex;
}

function lookup(rows, id) {
  const k = String(id).toLowerCase();
  return rows.find((r) => String(r.id).toLowerCase() === k);
}

export function createLiveFx(scene) {
  let items = []; // { group, sprite, light?, base, kind, phase }
  let time = 0;

  function clear() {
    for (const it of items) {
      scene.remove(it.sprite);
      it.sprite.material.dispose();
      if (it.light) scene.remove(it.light);
    }
    items = [];
  }

  function add(group, color, opacity, scale, kind, withLight, intensity) {
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture(), color, transparent: true, opacity, depthWrite: false, blending: THREE.AdditiveBlending,
    }));
    sprite.scale.setScalar(scale);
    sprite.raycast = () => {};
    scene.add(sprite);
    let light = null;
    if (withLight) {
      light = new THREE.PointLight(color, intensity, 7, 2);
      scene.add(light);
    }
    items.push({ group, sprite, light, base: opacity, kind, phase: Math.random() * 6 });
  }

  /** Rebuild the glows from a circuit build + its check report. */
  function updateCircuit(circuit, report, netlist) {
    clear();
    if (!circuit || !report || !Array.isArray(report.netlist)) return;
    for (const comp of netlist || []) {
      const group = circuit.meshesById.get(comp.id);
      const row = lookup(report.netlist, comp.id);
      if (!group || !row) continue;
      const type = String(comp.type).toLowerCase();
      if (type === 'led' && typeof row.current === 'number') {
        const frac = Math.min(1.5, Math.abs(row.current) / LED_FULL_A);
        if (frac < 0.02) continue;
        const color = ledColorHex(comp.color || 'red');
        add(group, color, 0.25 + 0.55 * Math.min(frac, 1), 1.6 + 1.6 * frac, 'led', true, 3 * frac);
      } else if (type === 'resistor' && typeof row.power === 'number') {
        const heat = Math.abs(row.power) / (comp.rating_watts || 0.25);
        if (heat < HEAT_START) continue;
        const t = Math.min(1.2, (heat - HEAT_START) / (1 - HEAT_START));
        add(group, HEAT_COLOR, 0.15 + 0.6 * Math.min(t, 1), 1.2 + 1.4 * t, 'heat', heat > 0.7, 1.5 * t);
      }
    }
  }

  const box = new THREE.Box3();
  const center = new THREE.Vector3();
  /** Per frame: follow the parts (fly-in, explode) and shimmer the heat. */
  function animate(dt) {
    time += dt;
    for (const it of items) {
      box.setFromObject(it.group);
      box.getCenter(center);
      center.y = box.max.y * 0.8;
      it.sprite.position.copy(center);
      if (it.light) it.light.position.copy(center);
      if (it.kind === 'heat') it.sprite.material.opacity = it.base * (0.8 + 0.2 * Math.sin(time * 5 + it.phase));
    }
  }

  return { updateCircuit, animate, clear };
}
