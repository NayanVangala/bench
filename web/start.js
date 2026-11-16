// Start screen: three bubbles (Circuit, Fiber link, Optics bench) fill the
// panel. Tapping one grows it; starting anything in it (Enter, Start, an
// image, an example) commits to that mode: the bubbles go away and the mode
// takes the whole panel. "New build" brings the bubbles back.
(function () {
  'use strict';
  var NAMES = { circuit: 'Circuit', fiber: 'Fiber link', optics: 'Optics bench' };
  // Each mode's own controls, which the bubbles hand their input to.
  var TARGET = {
    circuit: { goal: 'goal', file: 'file-input', go: 'check-btn', example: '[data-example="%s"]' },
    fiber: { goal: 'fiber-goal', file: 'fiber-file-input', go: 'fiber-design-btn', example: '[data-fiber-example="%s"]' },
    optics: { goal: 'optics-goal', file: 'optics-file-input', go: 'optics-design-btn', example: '[data-optics-example="%s"]' }
  };
  var bubbles = [].slice.call(document.querySelectorAll('.bubble'));
  function $(id) { return document.getElementById(id); }

  function expand(b) {
    bubbles.forEach(function (x) {
      x.classList.toggle('active', x === b);
      x.querySelector('.bubble-head').setAttribute('aria-expanded', x === b ? 'true' : 'false');
    });
  }

  function commit(dom) {
    document.body.setAttribute('data-committed', dom);
    $('mode-name').textContent = NAMES[dom];
    var t = $('build-title');
    if (t) t.placeholder = 'Untitled ' + NAMES[dom].toLowerCase();
    if (window.NetproofSetDomain) window.NetproofSetDomain(dom);
    if (dom === 'circuit') {
      var tab = $('tab-check');
      if (tab) tab.click();
    }
  }

  function newBuild() {
    document.body.removeAttribute('data-committed');
    window.dispatchEvent(new CustomEvent('bench:new-build'));
    bubbles.forEach(function (b) {
      b.querySelector('[data-start-goal]').value = '';
      b.querySelector('[data-start-input]').value = '';
      b.querySelector('[data-start-file]').hidden = true;
      b.querySelector('[data-start-image]').classList.remove('has-file');
    });
  }

  bubbles.forEach(function (b) {
    var dom = b.getAttribute('data-bubble');
    var t = TARGET[dom];
    var goal = b.querySelector('[data-start-goal]');
    var input = b.querySelector('[data-start-input]');
    var fileLine = b.querySelector('[data-start-file]');
    b.querySelector('.bubble-head').addEventListener('click', function () { expand(b); });
    b.addEventListener('focusin', function () { if (!b.classList.contains('active')) expand(b); });
    b.querySelector('[data-start-image]').addEventListener('click', function () { input.click(); });
    input.addEventListener('change', function () {
      var f = input.files && input.files[0];
      fileLine.hidden = !f;
      fileLine.textContent = f ? 'Image: ' + f.name : '';
      b.querySelector('[data-start-image]').classList.toggle('has-file', !!f);
    });

    function go() {
      var text = goal.value.trim();
      var file = input.files && input.files[0];
      if (!text && !file) { goal.focus(); goal.placeholder = 'Describe it first, or add an image'; return; }
      commit(dom);
      $(t.goal).value = text;
      if (file) {
        var dt = new DataTransfer();
        dt.items.add(file);
        $(t.file).files = dt.files;
        $(t.file).dispatchEvent(new Event('change', { bubbles: true }));
      }
      $(t.go).click();
    }
    b.querySelector('[data-start-go]').addEventListener('click', go);
    goal.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); }
    });
    b.querySelectorAll('[data-start-example]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        commit(dom);
        var target = document.querySelector(t.example.replace('%s', btn.getAttribute('data-start-example')));
        if (target) target.click();
      });
    });
  });

  $('new-build-btn').addEventListener('click', newBuild);
  window.NetproofStart = { commit: commit, newBuild: newBuild };
})();
