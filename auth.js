// Optional Sign in with Google (Supabase). Signed-in readers get read marks
// and "resume where you left off", synced across devices. Loaded only when
// the build has Supabase settings, and every call degrades to doing nothing.
(function () {
  var cfg = window.DC_AUTH;
  if (!cfg || !window.supabase) return;
  var sb = window.supabase.createClient(cfg.url, cfg.key);
  var btn = document.querySelector('[data-auth]');
  var menu = document.getElementById('account');
  var art = document.querySelector('.article');
  var slug = art ? art.dataset.slug : null;
  var user = null, reads = {};

  var esc = function (s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; };

  btn.hidden = false;
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    if (!user) {
      sb.auth.signInWithOAuth({ provider: 'google', options: { redirectTo: location.origin + location.pathname } });
      return;
    }
    menu.hidden = !menu.hidden;
  });
  menu.addEventListener('click', function (e) {
    e.stopPropagation();
    if (e.target.closest('[data-signout]')) sb.auth.signOut().then(function () { location.reload(); });
  });
  document.addEventListener('click', function () { menu.hidden = true; });

  function paintAccount() {
    if (!user) { btn.textContent = 'Sign in'; btn.className = 'signin'; return; }
    var meta = user.user_metadata || {};
    var name = meta.full_name || meta.name || user.email || '';
    btn.className = 'avatar';
    btn.setAttribute('aria-label', 'Account');
    btn.textContent = (name.trim()[0] || '?').toUpperCase();
    if (meta.avatar_url) btn.innerHTML = '<img src="' + esc(meta.avatar_url) + '" alt="" referrerpolicy="no-referrer">';
    var count = Object.keys(reads).filter(function (k) { return reads[k].read_at; }).length;
    menu.innerHTML = '<div><div style="font-weight:600">' + esc(name) + '</div><div class="muted" style="font-size:13px">' + esc(user.email || '') + '</div></div>'
      + '<div class="mono muted">' + count + ' deep dive' + (count === 1 ? '' : 's') + ' read</div>'
      + '<button type="button" class="btn btn-line" data-signout>Sign out</button>';
  }

  // Read marks on every list of reads, and "Continue" on the front page.
  function paintMarks() {
    document.querySelectorAll('[data-slug].row-item').forEach(function (row) {
      var r = reads[row.dataset.slug];
      row.classList.toggle('is-read', !!(r && r.read_at));
    });
    var start = document.querySelector('[data-start]');
    if (start) {
      var r = reads[start.dataset.slug], p = r ? r.progress : 0;
      if (r && r.read_at) start.firstChild.textContent = 'Read again ';
      else if (p > 0.02 && p < 0.97) start.firstChild.textContent = 'Continue · ' + Math.max(1, Math.round(start.dataset.minutes * (1 - p))) + ' min left ';
    }
    if (slug) paintArticle();
  }

  // Article page: mark-as-read button, resume button and progress saving.
  var markBtn = document.querySelector('[data-mark]');
  var resumeBtn = document.querySelector('[data-resume]');
  function paintArticle() {
    var r = reads[slug] || {};
    markBtn.hidden = false;
    markBtn.lastChild.textContent = r.read_at ? 'Mark unread' : 'Mark as read';
    markBtn.className = 'btn ' + (r.read_at ? 'btn-line' : 'btn-solid');
    if (!resumeBtn.dataset.used && !location.hash && r.progress > 0.02 && r.progress < 0.97 && progress() < 0.02) {
      resumeBtn.hidden = false;
      resumeBtn.textContent = 'Resume at ' + Math.round(r.progress * 100) + '%';
    }
  }
  function progress() {
    var rect = art.getBoundingClientRect();
    return Math.min(1, Math.max(0, -rect.top / Math.max(1, art.offsetHeight - window.innerHeight)));
  }
  function save(fields) {
    var row = Object.assign({ slug: slug }, reads[slug] || {}, fields, { updated_at: new Date().toISOString() });
    reads[slug] = row;
    sb.from('reads').upsert({ user_id: user.id, slug: slug, progress: row.progress || 0, read_at: row.read_at || null, updated_at: row.updated_at }).then(function () {});
  }
  if (slug) {
    markBtn.addEventListener('click', function () {
      if (!user) return;
      var on = !(reads[slug] && reads[slug].read_at);
      save({ read_at: on ? new Date().toISOString() : null });
      paintArticle();
    });
    resumeBtn.addEventListener('click', function () {
      var p = (reads[slug] || {}).progress || 0;
      var top = art.getBoundingClientRect().top + window.scrollY;
      window.scrollTo({ top: top + p * (art.offsetHeight - window.innerHeight) });
      resumeBtn.hidden = true;
      resumeBtn.dataset.used = '1';
    });
    var last = 0, lastP = -1, timer = 0;
    window.addEventListener('scroll', function () {
      if (!user) return;
      if (progress() > 0.04) { resumeBtn.hidden = true; resumeBtn.dataset.used = '1'; }
      clearTimeout(timer);
      timer = setTimeout(function () {
        var p = Math.round(progress() * 100) / 100;
        if (Math.abs(p - lastP) < 0.02 || Date.now() - last < 4000) return;
        last = Date.now(); lastP = p;
        var fields = { progress: p };
        if (p > 0.97 && !(reads[slug] && reads[slug].read_at)) fields.read_at = new Date().toISOString();
        save(fields);
        if (fields.read_at) paintArticle();
      }, 1200);
    }, { passive: true });
  }

  function load(session) {
    user = session ? session.user : null;
    paintAccount();
    if (!user) return;
    sb.from('reads').select('slug,progress,read_at').then(function (res) {
      reads = {};
      (res.data || []).forEach(function (r) { reads[r.slug] = r; });
      paintAccount();
      paintMarks();
    });
  }
  sb.auth.getSession().then(function (res) { load(res.data.session); });
  sb.auth.onAuthStateChange(function (event, session) {
    if (event === 'SIGNED_IN' || event === 'SIGNED_OUT') load(session);
  });
})();
