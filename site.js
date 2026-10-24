// Reading settings (Aa menu), reader progress and contents, copy buttons.
// Settings are a per-browser convenience, so every storage call may fail.
(function () {
  var root = document.documentElement;
  var get = function (k) { try { return localStorage.getItem('dc-' + k); } catch (e) { return null; } };
  var set = function (k, v) { try { localStorage.setItem('dc-' + k, v); } catch (e) {} };

  // Aa menu: text size and theme.
  var btn = document.querySelector('[data-settings]');
  var pop = document.getElementById('settings');
  if (btn && pop) {
    var mark = function () {
      var size = get('size') || '19', theme = root.dataset.theme || '';
      pop.querySelectorAll('[data-size]').forEach(function (b) { b.classList.toggle('on', b.dataset.size === size); });
      pop.querySelectorAll('[data-theme-pick]').forEach(function (b) { b.classList.toggle('on', b.dataset.themePick === theme); });
    };
    btn.addEventListener('click', function (e) { e.stopPropagation(); pop.hidden = !pop.hidden; btn.setAttribute('aria-expanded', String(!pop.hidden)); mark(); });
    pop.addEventListener('click', function (e) {
      e.stopPropagation();
      var b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.size) { root.style.setProperty('--read-size', b.dataset.size + 'px'); set('size', b.dataset.size); }
      if (b.dataset.themePick !== undefined) {
        if (b.dataset.themePick) root.dataset.theme = b.dataset.themePick; else delete root.dataset.theme;
        set('theme', b.dataset.themePick);
      }
      mark();
    });
    document.addEventListener('click', function () { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') pop.hidden = true; });
  }

  // Copy buttons on code blocks.
  document.querySelectorAll('.article pre').forEach(function (pre) {
    var b = document.createElement('button');
    b.className = 'copy';
    b.type = 'button';
    b.textContent = 'Copy';
    b.addEventListener('click', function () {
      var done = function () { b.textContent = 'Copied'; setTimeout(function () { b.textContent = 'Copy'; }, 1600); };
      if (navigator.clipboard) navigator.clipboard.writeText(pre.querySelector('code').innerText).then(done, done); else done();
    });
    pre.appendChild(b);
  });

  // Reader: progress bar, minutes left and the active contents entry.
  var art = document.querySelector('.article');
  var bar = document.querySelector('.progress');
  if (!art || !bar) return;
  var minutes = Number(art.dataset.minutes) || 0;
  var left = document.querySelector('[data-left]');
  var now = document.querySelector('[data-now]');
  var title = now ? now.textContent : '';
  var links = Array.prototype.slice.call(document.querySelectorAll('.toc a'));
  var heads = links.map(function (a) { return document.getElementById(a.hash.slice(1)); });
  var pct = document.querySelector('[data-pct]');
  var raf = 0;
  var update = function () {
    var r = art.getBoundingClientRect();
    var p = Math.min(1, Math.max(0, -r.top / Math.max(1, art.offsetHeight - window.innerHeight)));
    bar.style.width = (p * 100) + '%';
    var m = Math.max(0, Math.round(minutes * (1 - p)));
    if (left) left.textContent = m + ' min left';
    if (pct) pct.textContent = Math.round(p * 100) + '% · ' + m + ' min left';
    var cur = -1;
    heads.forEach(function (h, i) { if (h && h.getBoundingClientRect().top < 180) cur = i; });
    links.forEach(function (a, i) { a.classList.toggle('on', i === cur); a.classList.toggle('done', i < cur); });
    if (now) now.textContent = cur >= 0 && p > 0.04 ? links[cur].textContent : title;
  };
  window.addEventListener('scroll', function () { cancelAnimationFrame(raf); raf = requestAnimationFrame(update); }, { passive: true });
  window.addEventListener('resize', update);
  update();
})();
