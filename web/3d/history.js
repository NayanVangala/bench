/**
 * Netproof 3D repair history — web/3d/history.js
 *
 * Repair history in 3D: a timeline of rounds 1-3. Each round is a
 * button/chip (DOM) and a node on a Three.js timeline rail. Clicking a
 * round re-renders that round's netlist in the 3D scene via the
 * `onShowRound` callback and shows what changed in that round, one line
 * per change.
 *
 * No fake numbers: every verdict, finding, change line and component
 * value shown here comes from the backend reports / netlists passed in.
 * Nothing is invented, interpolated or guessed. When a round has no
 * report, the panel says so instead of showing a verdict. When a round
 * has no `changes`, the change lines are derived only by diffing that
 * round's real netlist against the previous round's real netlist; if
 * even that yields nothing, the panel says "No changes reported".
 *
 * Plain JS, no build step, no CDN. Three.js is expected as a global
 * (`window.THREE`, loaded by the host page before this file). If THREE
 * is absent, the DOM chips + change panel still work and `handle.group`
 * is null; the 3D timeline simply is not built.
 *
 * Data contract
 * ------------
 *   history: Array of {
 *     round:    number,            // backend repair round, 1..3
 *     netlist:  Array | Object,    // netlist rows [{id,type,value,nodes}]
 *                                // or backend IR {components:[...]}
 *     report:   Object | null,     // backend report {verdict, findings,
 *                                //   node_voltages, branch_currents, power}
 *     changes:  Array<string|Object>  // one entry per change; objects use
 *                                // .description / .message / .text
 *   }
 *
 *   callbacks: {
 *     onShowRound: function (roundIndex, netlist, report)
 *       // roundIndex is the 0-based index into the (round-sorted)
 *       // history array. The 1-based backend round is
 *       // history[roundIndex].round. netlist / report are passed
 *       // through exactly as supplied, un-normalised, so the host's
 *       // 3D renderer receives the shapes it already understands.
 *   }
 *   `callbacks` may also be a bare function, treated as onShowRound.
 *
 * Usage (host page owns the Three.js scene / renderer / render loop):
 *
 *   var handle = NetproofHistory.renderHistory(history, {
 *     onShowRound: function (i, netlist, report) {
 *       renderNetlist3D(netlist, report);   // host's 3D scene renderer
 *     }
 *   });
 *   document.getElementById('history-panel').appendChild(handle.element);
 *   handle.attachScene({ scene: scene, camera: camera, renderer: renderer });
 *   // handle.group is the THREE.Group timeline; attachScene() adds it
 *   // to the scene and wires raycast picking on the renderer canvas,
 *   // so clicking a 3D node selects that round too. If the host prefers
 *   // to add the group itself, skip attachScene() and just do
 *   // scene.add(handle.group) — the DOM chips still drive selection.
 *
 *   handle.select(2);            // show round index 2 (fires onShowRound)
 *   handle.update(newHistory);   // re-render with fresh backend data
 *   handle.destroy();            // remove listeners, dispose 3D objects
 *
 * Mounting: renderHistory(history, callbacks, mount?) — the optional
 * third argument is a DOM element (or CSS selector string) to append
 * the panel to. Without it, the caller appends handle.element itself.
 * Mounting does NOT call onShowRound: the newest round is selected
 * visually on mount (the host scene already shows the current netlist),
 * and the callback fires only on a real click / select() call.
 *
 * Colours match web/index.html verdict colours: PASS #1a7f37,
 * WARN #d97706, STOP #b00020, unknown/no report #777777.
 */

(function (root, factory) {
  'use strict';
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.NetproofHistory = factory();
  }
})(typeof window !== 'undefined' ? window : this, function () {
  'use strict';

  var MAX_ROUNDS = 3; // backend cap, see web/index.html MAX_REPAIR_ROUNDS
  var VERDICT_COLORS = {
    PASS: '#1a7f37',
    WARN: '#d97706',
    WARNING: '#d97706',
    STOP: '#b00020',
    FAIL: '#b00020',
    ERROR: '#b00020'
  };
  var UNKNOWN_COLOR = '#777777';
  var NODE_SPACING = 2.2; // Three.js world units between round nodes
  var NODE_RADIUS = 0.32;

  /* ------------------------------------------------------------------ */
  /* Small helpers                                                       */
  /* ------------------------------------------------------------------ */

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function isObject(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }

  function verdictOf(report) {
    if (!isObject(report) || report.verdict == null) return '';
    return String(report.verdict).toUpperCase();
  }

  function verdictColor(verdict) {
    return VERDICT_COLORS[verdict] || UNKNOWN_COLOR;
  }

  /** Unit suffix for a component value, by type. Formatting only: the
   *  numeric value itself is never altered. SI units per backend/netproof/ir.py. */
  function unitFor(type) {
    switch (String(type || '').toLowerCase()) {
      case 'resistor': return ' Ω';
      case 'capacitor': return ' F';
      case 'voltage_source': return ' V';
      case 'current_source': return ' A';
      case 'led':
      case 'diode': return ' V';
      default: return '';
    }
  }

  function formatValue(row) {
    if (row == null || row.value == null || row.value === '') return '';
    var n = Number(row.value);
    if (Number.isFinite(n)) return String(n) + unitFor(row.type);
    return String(row.value);
  }

  function componentLabel(row) {
    var id = row.id || '(no id)';
    var type = row.type ? ' ' + row.type : '';
    var value = formatValue(row);
    return id + type + (value ? ' ' + value : '');
  }

  /**
   * Normalise a netlist into rows [{id, type, value, nodes: [..]}].
   * Accepts the frontend row shape, the backend IR {components: [...]},
   * or {netlist: [...]}. Returns [] for anything else (never throws on
   * odd backend data; the panel then says the netlist is unavailable).
   */
  function normNetlist(netlist) {
    var rows = null;
    if (Array.isArray(netlist)) rows = netlist;
    else if (isObject(netlist) && Array.isArray(netlist.components)) rows = netlist.components;
    else if (isObject(netlist) && Array.isArray(netlist.netlist)) rows = netlist.netlist;
    else return [];
    return rows.map(function (r) {
      if (!isObject(r)) return { id: '', type: '', value: '', nodes: [] };
      var nodes = r.nodes;
      if (typeof nodes === 'string') nodes = nodes.split(/[\s,]+/).filter(Boolean);
      if (!Array.isArray(nodes)) nodes = [];
      return {
        id: r.id != null ? String(r.id) : (r.name != null ? String(r.name) : ''),
        type: r.type != null ? String(r.type) : (r.kind != null ? String(r.kind) : ''),
        value: r.value != null ? r.value : '',
        nodes: nodes.map(String)
      };
    });
  }

  /** One change entry -> one display line. Strings pass through verbatim;
   *  objects use .description / .message / .text, in that order. */
  function changeText(change) {
    if (typeof change === 'string') return change;
    if (isObject(change)) {
      var t = change.description || change.message || change.text || change.summary;
      if (typeof t === 'string' && t) return t;
      if (change.id && change.value != null) {
        return change.id + ': ' + String(change.value);
      }
    }
    return '';
  }

  function normChanges(entry) {
    var raw = entry.changes;
    if (raw == null) raw = entry.what_changed || entry.summary;
    if (typeof raw === 'string') return raw ? [raw] : [];
    if (!Array.isArray(raw)) return [];
    return raw.map(changeText).filter(function (s) { return s !== ''; });
  }

  /**
   * Derive change lines by diffing two real netlists (same component id,
   * value changed; component added; component removed). Only used when
   * the backend supplied no `changes` for a round. Every line is built
   * from values present in the two netlists — nothing invented.
   */
  function deriveChanges(prevNetlist, currNetlist) {
    var prev = {};
    normNetlist(prevNetlist).forEach(function (r) { if (r.id) prev[r.id] = r; });
    var lines = [];
    normNetlist(currNetlist).forEach(function (r) {
      if (!r.id) return;
      var old = prev[r.id];
      if (!old) {
        lines.push(r.id + ' added (' + componentLabel(r) + ')');
      } else if (String(old.value) !== String(r.value)) {
        lines.push(r.id + ': ' + formatValue(old) + ' -> ' + formatValue(r));
      }
      delete prev[r.id];
    });
    Object.keys(prev).forEach(function (id) {
      lines.push(id + ' removed (' + componentLabel(prev[id]) + ')');
    });
    return lines;
  }

  function normEntry(entry, index) {
    var e = isObject(entry) ? entry : {};
    var round = Number(e.round);
    if (!Number.isFinite(round)) round = index + 1; // 1-based fallback
    var report = isObject(e.report) ? e.report : null;
    // Some backends nest the report as {result: {...}} or attach the
    // verdict/findings at the entry top level; accept those shapes too.
    if (!report && isObject(e.result)) report = e.result;
    if (!report && (e.verdict != null || Array.isArray(e.findings))) {
      report = { verdict: e.verdict, findings: e.findings };
    }
    return {
      round: round,
      rawNetlist: e.netlist != null ? e.netlist : null,
      netlist: normNetlist(e.netlist),
      report: report,
      verdict: verdictOf(report),
      changes: normChanges(e)
    };
  }

  function normHistory(history) {
    if (!Array.isArray(history)) return [];
    return history
      .map(function (entry, i) { return normEntry(entry, i); })
      .sort(function (a, b) { return a.round - b.round; });
  }

  function findingsOf(report) {
    if (!isObject(report) || !Array.isArray(report.findings)) return [];
    return report.findings.map(function (f) {
      if (typeof f === 'string') return { severity: '', rule: '', message: f };
      if (!isObject(f)) return { severity: '', rule: '', message: String(f) };
      return {
        severity: String(f.severity || f.level || f.type || '').toUpperCase(),
        rule: String(f.rule_id || f.rule || f.code || ''),
        message: String(f.message || f.text || f.detail || ''),
        components: Array.isArray(f.component_ids)
          ? f.component_ids
          : (Array.isArray(f.components) ? f.components : [])
      };
    }).filter(function (f) { return f.message !== ''; });
  }

  /* ------------------------------------------------------------------ */
  /* Three.js timeline                                                   */
  /* ------------------------------------------------------------------ */

  function getThree() {
    if (typeof THREE !== 'undefined') return THREE;
    if (typeof window !== 'undefined' && window.THREE) return window.THREE;
    return null;
  }

  function makeTextSprite(THREE, text, opts) {
    opts = opts || {};
    var fontSize = opts.fontSize || 44;
    var color = opts.color || '#111111';
    var canvas = document.createElement('canvas');
    canvas.width = opts.width || 256;
    canvas.height = opts.height || 96;
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.font = 'bold ' + fontSize + 'px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = color;
    ctx.fillText(String(text), canvas.width / 2, canvas.height / 2);
    var texture = new THREE.CanvasTexture(canvas);
    texture.anisotropy = 4;
    var material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false });
    var sprite = new THREE.Sprite(material);
    var scale = opts.scale || 1;
    sprite.scale.set(scale * (canvas.width / canvas.height) * 0.55, scale * 0.55, 1);
    sprite.userData.isHistoryLabel = true;
    return sprite;
  }

  /**
   * Build the THREE.Group timeline: a rail along X, one verdict-coloured
   * node per round, a "Round N" label above and the verdict below each
   * node, and a selection ring on the active node. MeshBasicMaterial is
   * used deliberately (unlit) so the timeline stays readable under any
   * host scene lighting. Returns {group, nodes, ring} or null when
   * THREE is unavailable or there are no entries.
   */
  function buildTimelineGroup(THREE, entries, selectedIndex) {
    if (!THREE || !entries.length) return null;
    var group = new THREE.Group();
    group.name = 'netproof-repair-history';
    var n = entries.length;
    var span = (n - 1) * NODE_SPACING;
    var x0 = -span / 2;

    // Rail.
    var railGeo = new THREE.BoxGeometry(Math.max(span + 1.2, 1.2), 0.06, 0.06);
    var railMat = new THREE.MeshBasicMaterial({ color: 0x888888 });
    var rail = new THREE.Mesh(railGeo, railMat);
    rail.name = 'history-rail';
    group.add(rail);

    var nodes = [];
    entries.forEach(function (entry, i) {
      var x = x0 + i * NODE_SPACING;
      var color = verdictColor(entry.verdict);
      var nodeGeo = new THREE.SphereGeometry(NODE_RADIUS, 24, 16);
      var nodeMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(color) });
      var node = new THREE.Mesh(nodeGeo, nodeMat);
      node.position.set(x, 0, 0);
      node.name = 'history-round-' + entry.round;
      node.userData.roundIndex = i;
      node.userData.round = entry.round;
      group.add(node);
      nodes.push(node);

      var label = makeTextSprite(THREE, 'Round ' + entry.round, {
        color: '#111111', fontSize: 44, width: 256, height: 96, scale: 1
      });
      label.position.set(x, 0.85, 0);
      group.add(label);

      var verdictText = entry.verdict || 'NO REPORT';
      var verdictLabel = makeTextSprite(THREE, verdictText, {
        color: color, fontSize: 40, width: 256, height: 80, scale: 0.8
      });
      verdictLabel.position.set(x, -0.8, 0);
      group.add(verdictLabel);
    });

    // Selection ring (torus facing the camera, around the active node).
    var ringGeo = new THREE.TorusGeometry(NODE_RADIUS + 0.22, 0.05, 12, 40);
    var ringMat = new THREE.MeshBasicMaterial({ color: 0x111111 });
    var ring = new THREE.Mesh(ringGeo, ringMat);
    ring.name = 'history-selection-ring';
    ring.position.set(x0 + selectedIndex * NODE_SPACING, 0, 0);
    group.add(ring);

    return { group: group, nodes: nodes, ring: ring, x0: x0 };
  }

  function disposeTimeline(timeline) {
    if (!timeline) return;
    timeline.group.traverse(function (obj) {
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) {
        var mats = Array.isArray(obj.material) ? obj.material : [obj.material];
        mats.forEach(function (m) {
          if (m.map) m.map.dispose();
          m.dispose();
        });
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* DOM panel                                                           */
  /* ------------------------------------------------------------------ */

  function ensureStyle() {
    if (typeof document === 'undefined') return;
    if (document.getElementById('np-history-style')) return;
    var style = document.createElement('style');
    style.id = 'np-history-style';
    style.textContent = [
      '.np-history { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; color: #111; }',
      '.np-history-chips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }',
      '.np-history-chip { font: inherit; padding: 8px 14px; border-radius: 999px; border: 2px solid #111; background: #fff; color: #111; cursor: pointer; }',
      '.np-history-chip .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }',
      '.np-history-chip[aria-pressed="true"] { background: #111; color: #fff; }',
      '.np-history-chip:focus-visible { outline: 3px solid #2684ff; outline-offset: 2px; }',
      '.np-history-chip:disabled { opacity: .45; cursor: not-allowed; }',
      '.np-history-detail h3 { margin: 0 0 6px; font-size: 15px; }',
      '.np-history-verdict { font-weight: 800; }',
      '.np-history-changes, .np-history-findings, .np-history-components { margin: 6px 0; padding-left: 20px; }',
      '.np-history-changes li, .np-history-findings li, .np-history-components li { margin-bottom: 4px; }',
      '.np-history-empty { color: #777; }',
      '.np-history-note { font-size: 12px; color: #555; margin-top: 8px; }'
    ].join('\n');
    document.head.appendChild(style);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  /* ------------------------------------------------------------------ */
  /* renderHistory                                                       */
  /* ------------------------------------------------------------------ */

  function renderHistory(history, callbacks, mount) {
    if (typeof document === 'undefined') {
      throw new Error('NetproofHistory.renderHistory needs a DOM (document is undefined).');
    }
    var onShowRound = null;
    if (typeof callbacks === 'function') onShowRound = callbacks;
    else if (isObject(callbacks) && typeof callbacks.onShowRound === 'function') {
      onShowRound = callbacks.onShowRound;
    }

    var entries = normHistory(history);
    var state = {
      entries: entries,
      selectedIndex: entries.length ? entries.length - 1 : -1, // newest round
      onShowRound: onShowRound,
      timeline: null,
      threeAvailable: !!getThree(),
      scene: null,
      camera: null,
      renderer: null,
      destroyed: false
    };

    ensureStyle();

    var container = el('div', 'np-history');
    container.setAttribute('data-netproof-history', 'true');

    var chipsRow = el('div', 'np-history-chips');
    chipsRow.setAttribute('role', 'group');
    chipsRow.setAttribute('aria-label', 'Repair rounds');
    container.appendChild(chipsRow);

    var detail = el('div', 'np-history-detail');
    container.appendChild(detail);

    var note = el('p', 'np-history-note',
      state.threeAvailable
        ? 'Timeline also shown in the 3D scene; click a round node there, or a chip here.'
        : '3D timeline unavailable: Three.js (window.THREE) is not loaded. Chips below still work.');
    container.appendChild(note);

    var chipButtons = [];

    function changesFor(index) {
      var entry = entries[index];
      if (entry.changes.length) return entry.changes;
      if (index > 0) {
        var derived = deriveChanges(entries[index - 1].rawNetlist, entry.rawNetlist);
        if (derived.length) return derived;
      }
      return [];
    }

    function renderDetail() {
      detail.textContent = '';
      if (state.selectedIndex < 0 || !entries[state.selectedIndex]) {
        detail.appendChild(el('p', 'np-history-empty',
          'No repairs yet. Repair runs at most ' + MAX_ROUNDS + ' rounds.'));
        return;
      }
      var entry = entries[state.selectedIndex];
      var heading = el('h3', null, 'Round ' + entry.round);
      if (entry.verdict) {
        var v = el('span', 'np-history-verdict', ' — ' + entry.verdict);
        v.style.color = verdictColor(entry.verdict);
        heading.appendChild(v);
      } else {
        heading.appendChild(el('span', 'np-history-verdict', ' — no report for this round'));
      }
      detail.appendChild(heading);

      // What changed: one line per change, verbatim from the backend
      // (or diffed from the two real netlists when none were supplied).
      var changes = changesFor(state.selectedIndex);
      if (changes.length) {
        detail.appendChild(el('p', null, 'What changed:'));
        var ul = el('ul', 'np-history-changes');
        changes.forEach(function (line) { ul.appendChild(el('li', null, line)); });
        detail.appendChild(ul);
      } else {
        detail.appendChild(el('p', 'np-history-empty',
          'No changes reported for this round.'));
      }

      // Findings, verbatim from this round's backend report.
      var findings = findingsOf(entry.report);
      if (findings.length) {
        detail.appendChild(el('p', null, 'Findings:'));
        var ful = el('ul', 'np-history-findings');
        findings.forEach(function (f) {
          var label = (f.severity ? f.severity + ' ' : '') +
            (f.rule ? f.rule + ': ' : '') + f.message;
          ful.appendChild(el('li', null, label));
        });
        detail.appendChild(ful);
      } else if (entry.report) {
        detail.appendChild(el('p', 'np-history-empty', 'No findings in this round.'));
      }

      // Components in this round, as supplied (grounds what the 3D
      // scene is about to render; count is just the rows provided).
      if (entry.netlist.length) {
        detail.appendChild(el('p', null,
          'Components in this round (' + entry.netlist.length + '):'));
        var cul = el('ul', 'np-history-components');
        entry.netlist.forEach(function (row) {
          cul.appendChild(el('li', null, componentLabel(row)));
        });
        detail.appendChild(cul);
      } else {
        detail.appendChild(el('p', 'np-history-empty',
          'No netlist recorded for this round.'));
      }
    }

    function renderChips() {
      chipsRow.textContent = '';
      chipButtons = [];
      if (!entries.length) {
        chipsRow.appendChild(el('span', 'np-history-empty',
          'No repairs yet. Repair runs at most ' + MAX_ROUNDS + ' rounds.'));
        return;
      }
      entries.forEach(function (entry, i) {
        var btn = el('button', 'np-history-chip');
        btn.type = 'button';
        btn.setAttribute('aria-pressed', String(i === state.selectedIndex));
        btn.setAttribute('aria-label',
          'Show round ' + entry.round + ' netlist' +
          (entry.verdict ? ', verdict ' + entry.verdict : ', no report'));
        var dot = el('span', 'dot');
        dot.style.background = verdictColor(entry.verdict);
        btn.appendChild(dot);
        btn.appendChild(document.createTextNode(
          'Round ' + entry.round + (entry.verdict ? ' · ' + entry.verdict : '')));
        btn.addEventListener('click', function () { select(i, true); });
        chipsRow.appendChild(btn);
        chipButtons.push(btn);
      });
    }

    function moveSelectionRing() {
      if (!state.timeline || !state.timeline.ring) return;
      state.timeline.ring.position.x =
        state.timeline.x0 + state.selectedIndex * NODE_SPACING;
    }

    function refreshSelectionVisuals() {
      chipButtons.forEach(function (btn, i) {
        btn.setAttribute('aria-pressed', String(i === state.selectedIndex));
      });
      moveSelectionRing();
    }

    function select(index, fireCallback) {
      if (state.destroyed) return false;
      if (index < 0 || index >= entries.length) return false;
      state.selectedIndex = index;
      refreshSelectionVisuals();
      renderDetail();
      if (fireCallback && state.onShowRound) {
        var entry = entries[index];
        state.onShowRound(index, entry.rawNetlist, entry.report);
      }
      return true;
    }

    function buildTimeline() {
      var THREE = getThree();
      if (state.timeline) disposeTimeline(state.timeline);
      state.timeline = null;
      if (!THREE || !entries.length) return null;
      state.timeline = buildTimelineGroup(THREE, entries, state.selectedIndex);
      return state.timeline ? state.timeline.group : null;
    }

    var group = buildTimeline();

    var raycaster = null;
    var pointer = null;
    var onCanvasClick = null;
    var onCanvasHover = null;

    function attachScene(opts) {
      opts = opts || {};
      var scene = opts.scene;
      var camera = opts.camera;
      var renderer = opts.renderer;
      if (!scene || !camera || !renderer) {
        throw new Error('attachScene needs {scene, camera, renderer}.');
      }
      var THREE = getThree();
      if (!THREE) return false;
      if (!state.timeline) buildTimeline();
      if (state.timeline && state.timeline.group.parent !== scene) {
        scene.add(state.timeline.group);
      }
      state.scene = scene;
      state.camera = camera;
      state.renderer = renderer;
      raycaster = new THREE.Raycaster();
      pointer = new THREE.Vector2();
      var dom = renderer.domElement;
      onCanvasClick = function (event) {
        var rect = dom.getBoundingClientRect();
        pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
        raycaster.setFromCamera(pointer, camera);
        var hits = raycaster.intersectObjects(state.timeline.nodes, false);
        if (hits.length) select(hits[0].object.userData.roundIndex, true);
      };
      onCanvasHover = function (event) {
        var rect = dom.getBoundingClientRect();
        pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
        raycaster.setFromCamera(pointer, camera);
        var hits = raycaster.intersectObjects(state.timeline.nodes, false);
        dom.style.cursor = hits.length ? 'pointer' : '';
      };
      dom.addEventListener('click', onCanvasClick);
      dom.addEventListener('pointermove', onCanvasHover);
      return true;
    }

    function detachScene() {
      if (state.renderer && state.renderer.domElement) {
        var dom = state.renderer.domElement;
        if (onCanvasClick) dom.removeEventListener('click', onCanvasClick);
        if (onCanvasHover) dom.removeEventListener('pointermove', onCanvasHover);
        dom.style.cursor = '';
      }
      if (state.scene && state.timeline && state.timeline.group.parent === state.scene) {
        state.scene.remove(state.timeline.group);
      }
      state.scene = null;
      state.camera = null;
      state.renderer = null;
      onCanvasClick = null;
      onCanvasHover = null;
    }

    function update(newHistory) {
      if (state.destroyed) return false;
      var keepRound = state.selectedIndex >= 0 && entries[state.selectedIndex]
        ? entries[state.selectedIndex].round : null;
      entries = normHistory(newHistory);
      state.entries = entries;
      var keepIndex = keepRound != null
        ? entries.findIndex(function (e) { return e.round === keepRound; })
        : -1;
      state.selectedIndex = keepIndex >= 0
        ? keepIndex
        : (entries.length ? entries.length - 1 : -1);
      buildTimeline();
      if (state.scene && state.timeline) state.scene.add(state.timeline.group);
      renderChips();
      renderDetail();
      return true;
    }

    function destroy() {
      if (state.destroyed) return;
      state.destroyed = true;
      detachScene();
      disposeTimeline(state.timeline);
      state.timeline = null;
      if (container.parentNode) container.parentNode.removeChild(container);
    }

    renderChips();
    renderDetail();

    if (mount) {
      var target = typeof mount === 'string' ? document.querySelector(mount) : mount;
      if (!target) throw new Error('renderHistory mount target not found: ' + String(mount));
      target.appendChild(container);
    }

    return {
      element: container,
      group: group, // THREE.Group timeline, or null when THREE is absent
      threeAvailable: state.threeAvailable,
      select: select,
      getSelectedIndex: function () { return state.selectedIndex; },
      getEntries: function () { return state.entries.slice(); },
      attachScene: attachScene,
      detachScene: detachScene,
      update: update,
      destroy: destroy
    };
  }

  return {
    renderHistory: renderHistory,
    // Test-only pure helpers (no DOM / THREE needed).
    _internals: {
      normHistory: normHistory,
      normNetlist: normNetlist,
      normChanges: normChanges,
      deriveChanges: deriveChanges,
      formatValue: formatValue,
      componentLabel: componentLabel,
      verdictOf: verdictOf,
      verdictColor: verdictColor,
      findingsOf: findingsOf,
      MAX_ROUNDS: MAX_ROUNDS
    }
  };
});
