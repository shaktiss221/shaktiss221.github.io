// Becoming AI First: which days are done, today's session and the streak.
// Progress lives only in this browser, so every storage call may fail.
(function () {
  var KEY = 'dc-aif';
  var state = {};
  try { state = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { state = {}; }
  if (!state.done || typeof state.done !== 'object') state.done = {};
  var save = function () { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} };

  var pad = function (n) { return n < 10 ? '0' + n : String(n); };
  var ymd = function (d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); };
  var href = function (n) { return '/ai-first/day-' + pad(n) + '.html'; };
  var all = function (sel, fn) { Array.prototype.forEach.call(document.querySelectorAll(sel), fn); };

  var days = [];
  var data = document.getElementById('aif-days');
  try { if (data) days = JSON.parse(data.textContent); } catch (e) { days = []; }
  var totalEl = document.querySelector('[data-aif-total]');
  var total = days.length || (totalEl ? Number(totalEl.getAttribute('data-aif-total')) : 0);

  // Consecutive calendar days with at least one session, ending today (or yesterday,
  // so the streak doesn't look broken before today's session).
  var streak = function () {
    var dates = {};
    Object.keys(state.done).forEach(function (k) { if (state.done[k]) dates[state.done[k]] = true; });
    var d = new Date();
    if (!dates[ymd(d)]) d.setDate(d.getDate() - 1);
    var n = 0;
    while (dates[ymd(d)]) { n++; d.setDate(d.getDate() - 1); }
    return n;
  };

  var render = function () {
    var count = Object.keys(state.done).filter(function (k) { return state.done[k]; }).length;
    var next = 0;
    for (var i = 1; i <= total; i++) { if (!state.done[i]) { next = i; break; } }

    all('[data-day].row-item', function (el) { el.classList.toggle('is-read', !!state.done[el.getAttribute('data-day')]); });
    if (total) all('[data-aif-progress]', function (el) { el.textContent = count + ' of ' + total + ' days done'; });
    all('[data-aif-bar]', function (el) { el.style.width = (total ? Math.round(count / total * 100) : 0) + '%'; });
    var s = streak();
    all('[data-aif-streak]', function (el) { el.textContent = s ? s + '-day streak' : ''; });

    if (days.length) {
      var day = next ? days[next - 1] : null;
      all('[data-aif-today-link]', function (el) { el.setAttribute('href', next ? href(next) : '/ai-first/'); });
      all('[data-aif-today-day]', function (el) { el.textContent = next ? 'Day ' + next + ' · Week ' + day.w : 'All ' + total + ' days done'; });
      all('[data-aif-today-title]', function (el) { el.textContent = next ? day.t : 'Pick your next 90 days'; });
    }

    all('[data-aif-mark]', function (b) {
      var n = b.getAttribute('data-day');
      var done = !!state.done[n];
      b.classList.toggle('is-done', done);
      b.querySelector('span').textContent = done ? 'Done · undo' : 'Mark day ' + n + ' done';
    });
  };

  all('[data-aif-mark]', function (b) {
    b.addEventListener('click', function () {
      var n = b.getAttribute('data-day');
      if (state.done[n]) delete state.done[n]; else state.done[n] = ymd(new Date());
      save();
      render();
    });
  });
  render();
})();
