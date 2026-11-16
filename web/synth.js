// Live synthesis + tuning view (BEAMTEAM-style), shared by all three modes.
//
//  BenchSynth  - a log panel over the bench: step strip, timer, live score
//                sparkline and the event log. Also floats the user's source
//                image above the 3D bench while the AI reads it, then flies
//                the parts in.
//  BenchTuner  - runs the deterministic tuner (POST /tune/<mode>, streamed)
//                with the user's preferences and applies the result.
// Every event shown comes from the server; nothing is simulated here.
(function () {
  'use strict';
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  var STEPS = [['read', 'Read'], ['ai', 'AI'], ['validate', 'Validate'], ['check', 'Check'], ['flyin', 'Fly-in'], ['tune', 'Tune']];
  var STAGE_TO_STEP = { input: 'read', model: 'ai', wait: 'ai', retry: 'ai', parse: 'validate', verify: 'check', autofix: 'tune', tune: 'tune' };
  var t0 = 0, timer = null, scores = [], imageUrl = null, active = null;

  function h3d() { return window.__netproof3d; }
  function fmt(s) { var m = Math.floor(s / 60), r = Math.floor(s % 60); return m + ':' + (r < 10 ? '0' : '') + r; }

  function setStep(key, state) {
    var li = document.querySelector('#synth-steps [data-step="' + key + '"]');
    if (!li) return;
    // Everything before a running/done step is done.
    if (state === 'running' || state === 'done') {
      var past = true;
      STEPS.forEach(function (s) {
        if (s[0] === key) past = false;
        var el = document.querySelector('#synth-steps [data-step="' + s[0] + '"]');
        if (past && el && el.className !== 'skip') el.className = 'done';
      });
    }
    li.className = state;
  }

  function spark() {
    var svg = $('synth-spark');
    if (scores.length < 2) { svg.innerHTML = ''; return; }
    var lo = Math.min.apply(null, scores), hi = Math.max.apply(null, scores);
    if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
    var pts = scores.map(function (s, i) {
      return (i * 300 / (scores.length - 1)).toFixed(1) + ',' + (34 - 30 * (s - lo) / (hi - lo)).toFixed(1);
    }).join(' ');
    svg.innerHTML = '<polyline points="' + pts + '"/><text x="300" y="10" text-anchor="end">score ' + scores[scores.length - 1].toFixed(1) + '</text>';
  }

  function log(stage, message, kind) {
    var box = $('synth-log');
    var t = t0 ? (Date.now() - t0) / 1000 : 0;
    var last = box.lastElementChild;
    if (kind === 'tick' && last && last.classList.contains('tick')) {
      last.innerHTML = '<span class="t">' + fmt(t) + '</span> <span class="s">' + esc(stage) + '</span> ' + esc(message);
      return;
    }
    var div = document.createElement('div');
    div.className = 'line' + (kind ? ' ' + kind : '');
    div.innerHTML = '<span class="t">' + fmt(t) + '</span> <span class="s">' + esc(stage) + '</span> ' + esc(message);
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  var BenchSynth = {
    // opts: { title, image: File|null, skip: [stepKeys] }
    start: function (opts) {
      opts = opts || {};
      active = true;
      $('synth').hidden = false;
      $('synth').classList.remove('min');
      $('synth-title').textContent = opts.title || 'Synthesizing';
      $('synth-log').innerHTML = '';
      scores = [];
      spark();
      $('synth-steps').innerHTML = STEPS.map(function (s) {
        return '<li data-step="' + s[0] + '" class="' + ((opts.skip || []).indexOf(s[0]) >= 0 ? 'skip' : '') + '">' + s[1] + '</li>';
      }).join('');
      t0 = Date.now();
      clearInterval(timer);
      timer = setInterval(function () { $('synth-timer').textContent = fmt((Date.now() - t0) / 1000); }, 250);
      if (opts.image) {
        if (imageUrl) URL.revokeObjectURL(imageUrl);
        imageUrl = URL.createObjectURL(opts.image);
        if (h3d() && h3d().showReference) h3d().showReference(imageUrl);
        setStep('read', 'running');
        log('read', 'Placed ' + opts.image.name + ' over the bench');
      }
    },
    event: function (ev) {
      if (!active) return;
      var step = STAGE_TO_STEP[ev.stage];
      if (step) setStep(step, 'running');
      if (typeof ev.score === 'number') { scores.push(ev.score); spark(); }
      if (ev.message) log(ev.agent || ev.stage, ev.message, ev.tick ? 'tick' : (ev.stage === 'error' ? 'err' : (ev.agent === 'Judge' ? 'ok' : '')));
    },
    step: setStep,
    log: function (stage, message) {
      // The circuit console also logs raw SSE bookkeeping ("event: progress"); skip it here.
      if (active && !/^event: /.test(String(message))) log(stage, message);
    },
    finish: function (ok, message) {
      clearInterval(timer);
      active = false;
      if (message) log(ok ? 'done' : 'error', message, ok ? 'ok' : 'err');
      document.querySelectorAll('#synth-steps li').forEach(function (li) {
        if (li.className === 'running') li.className = ok ? 'done' : 'failed';
      });
      $('synth-title').textContent = ok ? 'Done' : 'Stopped';
    },
    flyIn: function (bboxes) {
      var h = h3d();
      if (!h || !h.flyIn) return;
      setStep('flyin', 'running');
      h.flyIn(bboxes || {});
      setTimeout(function () {
        setStep('flyin', 'done');
        if (h.fadeReference) h.fadeReference(0.14);
      }, 1500);
    },
    clearImage: function () { var h = h3d(); if (h && h.clearReference) h.clearReference(); },

    // POST and read a server-sent event stream; returns the 'done' event.
    stream: async function (url, init, onEvent) {
      var res = await fetch(url, init);
      if (!res.ok) {
        var data = {};
        try { data = await res.json(); } catch (e) { /* non-JSON */ }
        throw new Error(data.message || ('Request failed (' + res.status + ')'));
      }
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = '', done = null;
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        var lines = buf.split('\n');
        buf = lines.pop();
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i].trim();
          if (line.indexOf('data:') !== 0) continue;
          var ev;
          try { ev = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
          if (ev.stage === 'error') throw new Error(ev.message || 'The run failed.');
          if (ev.stage === 'done') done = ev;
          else if (onEvent) onEvent(ev);
        }
      }
      if (!done) throw new Error('The run ended without a result.');
      return done;
    }
  };
  window.BenchSynth = BenchSynth;
  $('synth-min').addEventListener('click', function () { $('synth').classList.toggle('min'); });
  $('synth-close').addEventListener('click', function () { $('synth').hidden = true; BenchSynth.clearImage(); });

  // ---- Tuner -----------------------------------------------------------
  // Per-mode adapters: read the current design, apply a tuned one.
  var MODES = {
    circuit: {
      get: function () { return window.NetproofPage ? { components: window.NetproofPage.getNetlist() } : null; },
      apply: function (d) { window.NetproofPage.applyNetlist(d.components); window.NetproofPage.recheck(); }
    },
    fiber: {
      get: function () { return window.NetproofFiber && window.NetproofFiber.get(); },
      apply: function (d, r) { window.NetproofFiber.apply(d, r); }
    },
    optics: {
      get: function () { return window.NetproofOptics && window.NetproofOptics.get(); },
      apply: function (d, r) { window.NetproofOptics.apply(d, r); }
    }
  };

  var BenchTuner = {
    run: async function (mode, opts) {
      opts = opts || {};
      var card = document.querySelector('[data-tuner="' + mode + '"]');
      var design = MODES[mode].get();
      if (!card || !design || (mode === 'circuit' && !design.components.length)) return;
      var target = card.querySelector('[data-tune-target]');
      var prefs = { objective: card.querySelector('[data-tune-objective]').value };
      if (target && target.value !== '') prefs[mode === 'circuit' ? 'target_ma' : 'target_dbm'] = Number(target.value);
      var btn = card.querySelector('[data-tune-go]');
      btn.disabled = true;
      if (!opts.continueRun) BenchSynth.start({ title: 'Tuning', skip: ['read', 'ai', 'validate', 'check', 'flyin'] });
      else { active = true; $('synth-title').textContent = 'Tuning'; }
      setStep('tune', 'running');
      try {
        var done = await BenchSynth.stream('/tune/' + mode, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ design: design, prefs: prefs })
        }, BenchSynth.event);
        MODES[mode].apply(done.design, done.report);
        card.querySelector('[data-tune-score]').textContent = done.score_before.toFixed(1) + ' → ' + done.score_after.toFixed(1);
        card.querySelector('[data-tune-changes]').innerHTML = done.changes.length
          ? done.changes.map(function (c) { return '<li>' + esc(c) + '</li>'; }).join('')
          : '<li class="empty">Already optimal for these preferences.</li>';
        BenchSynth.finish(true, 'Tuned: score ' + done.score_before.toFixed(1) + ' → ' + done.score_after.toFixed(1) +
          (done.changes.length ? ' (' + done.changes.length + ' change' + (done.changes.length > 1 ? 's' : '') + ')' : ''));
      } catch (e) {
        BenchSynth.finish(false, e.message);
      } finally {
        btn.disabled = false;
      }
    },
    auto: function (mode) {
      var card = document.querySelector('[data-tuner="' + mode + '"]');
      return !!(card && card.querySelector('[data-tune-auto]').checked);
    }
  };
  window.BenchTuner = BenchTuner;
  document.querySelectorAll('[data-tuner]').forEach(function (card) {
    card.querySelector('[data-tune-go]').addEventListener('click', function () { BenchTuner.run(card.getAttribute('data-tuner')); });
  });
})();
