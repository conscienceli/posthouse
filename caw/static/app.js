/* Cross-Agent Workspace admin UI — dependency-free single-page app. */
(function () {
  'use strict';

  // ------------------------------------------------------------------ i18n
  // Languages live in /static/i18n/<code>.json and are listed in /static/i18n/languages.json.
  // English is the default and the fallback for any missing key.
  const I18N = {};
  let LANGS = [{ code: 'en', name: 'English' }];
  let lang = 'en';

  async function loadLang(code) {
    if (I18N[code]) return true;
    try {
      const r = await fetch('/static/i18n/' + encodeURIComponent(code) + '.json', { cache: 'no-cache' });
      if (!r.ok) return false;
      I18N[code] = await r.json();
      return true;
    } catch (e) { return false; }
  }

  async function initI18n() {
    try {
      const r = await fetch('/static/i18n/languages.json', { cache: 'no-cache' });
      if (r.ok) LANGS = await r.json();
    } catch (e) { /* keep the default list */ }
    await loadLang('en');
    let saved = null;
    try { saved = localStorage.getItem('caw.lang'); } catch (e) { /* storage unavailable */ }
    if (saved === 'zh') saved = 'zh-CN';  // value used by early versions
    if (saved && saved !== 'en' && LANGS.some((l) => l.code === saved) && (await loadLang(saved))) lang = saved;
    document.documentElement.lang = lang;
  }

  function t(key, vars) {
    let s = (I18N[lang] && I18N[lang][key]) || (I18N.en && I18N.en[key]) || key;
    if (vars) for (const [k, v] of Object.entries(vars)) s = s.split('{' + k + '}').join(String(v));
    return s;
  }

  async function setLang(code) {
    if (!(await loadLang(code))) { toast('Language file not found: ' + code, true); return; }
    lang = code;
    try { localStorage.setItem('caw.lang', code); } catch (e) { /* ignore */ }
    document.documentElement.lang = code;
    render();
  }

  function langSelect() {
    const sel = h('select', { class: 'lang-select', 'aria-label': 'Language' },
      LANGS.map((l) => h('option', { value: l.code }, l.name)));
    sel.value = lang;
    sel.addEventListener('change', () => setLang(sel.value));
    return sel;
  }
  const locale = () => lang;

  // ------------------------------------------------------------------- DOM
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'value') el.value = v;
        else if (k === 'checked') el.checked = !!v;
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
        else el.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const $app = document.getElementById('app');
  const $modalRoot = document.getElementById('modal-root');
  const $toasts = document.getElementById('toasts');

  function toast(msg, isErr) {
    const el = h('div', { class: 'toast' + (isErr ? ' err' : '') }, msg);
    $toasts.append(el);
    setTimeout(() => el.remove(), isErr ? 6000 : 2600);
  }

  // ------------------------------------------------------------------- API
  async function api(method, path, body, opts) {
    opts = opts || {};
    const headers = { 'X-CAW-CSRF': '1' };
    let payload;
    if (opts.form) payload = opts.form;
    else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
    const r = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' });
    if (!r.ok) {
      let data = {};
      try { data = await r.json(); } catch (e) { /* not json */ }
      const err = new Error(data.message || r.statusText || ('HTTP ' + r.status));
      err.status = r.status; err.data = data;
      if (r.status === 401 && path !== '/api/login') { state.me = null; render(); }
      throw err;
    }
    if (opts.raw) return r;
    const ct = r.headers.get('content-type') || '';
    return ct.includes('application/json') ? r.json() : r.text();
  }
  const enc = encodeURIComponent;
  const wsApi = (name) => '/api/workspaces/' + enc(name);
  const encPath = (p) => p.split('/').map(enc).join('/');

  // ------------------------------------------------------------ formatting
  function fmtBytes(n) {
    if (n === null || n === undefined) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }
  function absTime(iso) { return iso ? new Date(iso).toLocaleString(locale()) : '—'; }
  function relTime(iso) {
    if (!iso) return '—';
    const sec = (new Date(iso).getTime() - Date.now()) / 1000;
    if (Math.abs(sec) < 45) return t('time.justNow');
    const rtf = new Intl.RelativeTimeFormat(locale(), { numeric: 'auto' });
    const units = [['year', 31536000], ['month', 2592000], ['day', 86400], ['hour', 3600], ['minute', 60]];
    for (const [u, s] of units) if (Math.abs(sec) >= s) return rtf.format(Math.round(sec / s), u);
    return rtf.format(Math.round(sec), 'second');
  }
  function timeEl(iso) { return h('span', { title: absTime(iso) }, relTime(iso)); }
  function colorFor(name) {
    let x = 0;
    for (const c of String(name)) x = (x * 31 + c.codePointAt(0)) >>> 0;
    return 'hsl(' + (x % 360) + ' 55% 48%)';
  }
  function genPassword() {
    const a = new Uint8Array(15);
    crypto.getRandomValues(a);
    return btoa(String.fromCharCode(...a)).replace(/[+/=]/g, '').slice(0, 20);
  }

  // ---------------------------------------------------------------- modals
  function modal(opts) {
    const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); if (opts.onClose) opts.onClose(); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    const box = h('div', { class: 'modal' + (opts.wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true' },
      h('div', { class: 'modal-head' }, h('h2', null, opts.title),
        h('button', { class: 'btn ghost sm', onclick: close, 'aria-label': t('common.close') }, '✕')),
      h('div', { class: 'modal-body' }, opts.body),
      opts.foot ? h('div', { class: 'modal-foot' }, opts.foot) : null);
    const backdrop = h('div', { class: 'modal-backdrop', onmousedown: (e) => { if (e.target === backdrop) close(); } }, box);
    document.addEventListener('keydown', onKey);
    $modalRoot.append(backdrop);
    const first = box.querySelector('input, textarea, select');
    if (first) setTimeout(() => first.focus(), 30);
    return { close, box };
  }

  function confirmDialog(text, okText, danger) {
    return new Promise((resolve) => {
      let done = false;
      const m = modal({
        title: t('common.confirm'), body: h('p', null, text),
        onClose: () => { if (!done) resolve(false); },
        foot: [h('button', { class: 'btn', onclick: () => m.close() }, t('common.cancel')),
          h('button', { class: 'btn ' + (danger ? 'danger solid' : 'primary'), onclick: () => { done = true; resolve(true); m.close(); } },
            okText || t('common.confirm'))],
      });
    });
  }

  function promptDialog(title, label, value, hint, okText) {
    return new Promise((resolve) => {
      let done = false;
      const input = h('input', { type: 'text', value: value || '' });
      const submit = () => { done = true; resolve(input.value.trim()); m.close(); };
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
      const m = modal({
        title, onClose: () => { if (!done) resolve(null); },
        body: h('label', { class: 'field' }, h('span', null, label), input, hint ? h('div', { class: 'hint' }, hint) : null),
        foot: [h('button', { class: 'btn', onclick: () => m.close() }, t('common.cancel')),
          h('button', { class: 'btn primary', onclick: submit }, okText || t('common.save'))],
      });
      setTimeout(() => { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }, 40);
    });
  }

  function copyBox(text, multiline) {
    const btn = h('button', { class: 'btn sm', onclick: async () => {
      try { await navigator.clipboard.writeText(text); btn.textContent = t('common.copied'); }
      catch (e) { toast(text); }
      setTimeout(() => { btn.textContent = t('common.copy'); }, 1500);
    } }, t('common.copy'));
    return h('div', { class: 'copybox' }, h(multiline ? 'pre' : 'code', null, text), btn);
  }

  // ----------------------------------------------------------------- state
  const state = {
    me: null, info: null, checked: false,
    files: {},        // per workspace: { entries, expanded:Set, dir, loaded }
    editor: null,     // { ws, path, version, original, encoding, size, revision }
    suppressHash: false, lastHash: location.hash,
  };
  let renderSeq = 0;

  function isDirty() {
    const ed = state.editor;
    if (!ed || ed.encoding !== 'utf-8') return false;
    const ta = document.getElementById('editor-text');
    return !!ta && ta.value !== ed.original;
  }

  window.addEventListener('beforeunload', (e) => { if (isDirty()) { e.preventDefault(); e.returnValue = ''; } });
  window.addEventListener('hashchange', () => {
    if (state.suppressHash) { state.suppressHash = false; return; }
    if (isDirty() && !window.confirm(t('files.leave'))) {
      state.suppressHash = true;
      location.hash = state.lastHash;
      return;
    }
    state.lastHash = location.hash;
    render();
  });
  function go(hash) { if (location.hash === hash) render(); else location.hash = hash; }

  // ---------------------------------------------------------------- router
  function parseRoute() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter((x) => x !== '').map(decodeURIComponent);
    if (!parts.length) return { page: 'workspaces' };
    if (parts[0] === 'w' && parts[1]) {
      const ws = parts[1];
      if (parts[2] === 'f') return { page: 'workspace', ws, tab: 'files', file: parts.slice(3).join('/') };
      return { page: 'workspace', ws, tab: parts[2] || 'files' };
    }
    return { page: parts[0] };
  }
  const fileHash = (ws, path) => '#/w/' + enc(ws) + '/f/' + encPath(path);
  const wsHash = (ws, tab) => '#/w/' + enc(ws) + (tab && tab !== 'files' ? '/' + tab : '');

  async function render() {
    const seq = ++renderSeq;
    if (!state.checked) {
      try { state.info = await api('GET', '/api/info'); } catch (e) { state.info = null; }
      try { state.me = await api('GET', '/api/me'); } catch (e) { state.me = null; }
      state.checked = true;
    }
    if (seq !== renderSeq) return;
    if (!state.me || state.me.role !== 'admin') return renderLogin();
    const route = parseRoute();
    const main = h('main');
    $app.replaceChildren(topbar(route), main);
    const pages = { workspaces: pageWorkspaces, workspace: pageWorkspace, tokens: pageTokens,
      connections: pageConnections, activity: pageActivity, trash: pageTrash, connect: pageConnect };
    const fn = pages[route.page] || pageWorkspaces;
    main.append(h('div', { class: 'muted' }, t('common.loading')));
    try {
      const content = await fn(route, () => seq !== renderSeq);
      if (seq !== renderSeq) return;
      main.replaceChildren(content);
    } catch (e) {
      if (seq !== renderSeq) return;
      main.replaceChildren(h('div', { class: 'card' }, h('h2', null, t('common.error')), h('p', null, e.message)));
    }
  }

  function topbar(route) {
    const links = [['workspaces', '#/'], ['tokens', '#/tokens'], ['connections', '#/connections'],
      ['activity', '#/activity'], ['trash', '#/trash'], ['connect', '#/connect']];
    const active = route.page === 'workspace' ? 'workspaces' : route.page;
    return h('header', { class: 'topbar' },
      h('a', { class: 'brand', href: '#/' }, h('img', { src: '/static/favicon.svg', alt: '' }), h('span', null, t('app.name'))),
      h('nav', { class: 'nav' }, links.map(([k, href]) =>
        h('a', { href, class: active === k ? 'active' : '' }, t('nav.' + k)))),
      h('div', { class: 'right' }, langSelect(),
        h('button', { class: 'btn ghost sm', onclick: logout }, t('nav.logout'))));
  }

  async function logout() {
    try { await api('POST', '/api/logout'); } catch (e) { /* ignore */ }
    state.me = null; state.files = {}; state.editor = null;
    render();
  }

  // ----------------------------------------------------------------- login
  function renderLogin() {
    const pw = h('input', { type: 'password', autocomplete: 'current-password', required: true });
    const err = h('div', { class: 'err-text' });
    const submit = async (e) => {
      e.preventDefault();
      err.textContent = '';
      try {
        await api('POST', '/api/login', { password: pw.value });
        state.me = await api('GET', '/api/me');
        render();
      } catch (ex) { err.textContent = t('login.failed') + ': ' + ex.message; }
    };
    $app.replaceChildren(h('div', { class: 'login-wrap' },
      h('form', { class: 'card login stack', onsubmit: submit },
        h('div', { class: 'brand' }, h('img', { src: '/static/favicon.svg', alt: '' }), t('app.name')),
        h('h1', null, t('login.title')), h('p', { class: 'muted' }, t('login.subtitle')),
        h('label', { class: 'field' }, h('span', null, t('login.password')), pw), err,
        h('div', { class: 'row' },
          h('button', { class: 'btn primary', type: 'submit' }, t('login.submit')), h('div', { class: 'grow' }),
          langSelect()))));
    setTimeout(() => pw.focus(), 30);
  }

  // ------------------------------------------------------------ workspaces
  async function pageWorkspaces() {
    const data = await api('GET', '/api/workspaces');
    const filter = h('input', { type: 'search', placeholder: t('ws.search'), style: 'max-width:260px' });
    const grid = h('div', { class: 'grid' });
    const draw = () => {
      const q = filter.value.trim().toLowerCase();
      const items = data.workspaces.filter((w) => !q || w.name.toLowerCase().includes(q) || (w.description || '').toLowerCase().includes(q));
      grid.replaceChildren(...items.map(wsCard));
      if (!data.workspaces.length) grid.replaceChildren(h('div', { class: 'empty', style: 'grid-column:1/-1' }, t('ws.empty')));
    };
    filter.addEventListener('input', draw);
    draw();
    return h('div', null,
      h('div', { class: 'page-head' },
        h('div', { class: 'grow' }, h('h1', null, t('ws.title'), ' ', h('span', { class: 'faint' }, '(' + data.workspaces.length + ')'))),
        filter, h('button', { class: 'btn primary', onclick: newWorkspaceDialog }, '+ ' + t('ws.new'))),
      grid);
  }

  function wsCard(w) {
    return h('a', { class: 'card ws-card', href: wsHash(w.name) },
      h('div', { class: 'row' }, h('div', { class: 'title grow' }, w.name),
        w.has_password ? h('span', { class: 'badge warn', title: t('ws.protected') }, '🔒 ' + t('ws.protected')) : null),
      h('div', { class: 'desc' }, w.description || '—'),
      h('div', { class: 'meta' }, h('span', null, t('ws.files', { n: w.file_count })), h('span', null, fmtBytes(w.total_bytes)),
        h('span', { title: absTime(w.updated_at) }, t('ws.updated', { t: relTime(w.updated_at) }))));
  }

  function newWorkspaceDialog() {
    const name = h('input', { type: 'text', autocomplete: 'off' });
    const desc = h('textarea', { rows: 3 });
    const pw = h('input', { type: 'text', autocomplete: 'off' });
    const tpl = h('input', { type: 'checkbox', checked: true });
    const err = h('div', { class: 'err-text' });
    const submit = async () => {
      err.textContent = '';
      try {
        const ws = await api('POST', '/api/workspaces', { name: name.value.trim(), description: desc.value.trim(), password: pw.value || null, template: tpl.checked });
        m.close(); toast(t('ws.created')); go(wsHash(ws.name));
      } catch (e) { err.textContent = e.message; }
    };
    const m = modal({
      title: t('ws.new'),
      body: [
        h('label', { class: 'field' }, h('span', null, t('common.name')), name, h('div', { class: 'hint' }, t('ws.nameHint'))),
        h('label', { class: 'field' }, h('span', null, t('common.description')), desc),
        h('label', { class: 'field' }, h('span', null, t('common.password'), ' ', h('span', { class: 'faint' }, '(' + t('common.optional') + ')')),
          h('div', { class: 'row' }, pw, h('button', { class: 'btn', type: 'button', onclick: () => { pw.value = genPassword(); } }, t('common.generate'))),
          h('div', { class: 'hint' }, t('ws.passwordHint'))),
        h('label', { class: 'check' }, tpl, h('span', null, t('ws.template'))), err],
      foot: [h('button', { class: 'btn', onclick: () => m.close() }, t('common.cancel')),
        h('button', { class: 'btn primary', onclick: submit }, t('common.create'))],
    });
  }

  // ------------------------------------------------------------- workspace
  async function pageWorkspace(route, stale) {
    const ws = await api('GET', wsApi(route.ws));
    if (stale()) return h('div');
    const tabs = ['files', 'history', 'activity', 'settings', 'connect'];
    const tab = tabs.includes(route.tab) ? route.tab : 'files';
    const head = h('div', { class: 'page-head' },
      h('div', { class: 'grow' },
        h('div', { class: 'crumbs' }, h('a', { href: '#/' }, t('nav.workspaces')), ' / '),
        h('div', { class: 'row wrap' }, h('h1', null, ws.name),
          ws.has_password ? h('span', { class: 'badge warn' }, '🔒 ' + t('ws.protected')) : h('span', { class: 'badge' }, t('ws.noPassword')),
          h('span', { class: 'badge' }, t('ws.files', { n: ws.file_count }) + ' · ' + fmtBytes(ws.total_bytes))),
        ws.description ? h('div', { class: 'muted', style: 'margin-top:4px;white-space:pre-wrap' }, ws.description) : null));
    const tabBar = h('div', { class: 'tabs' }, tabs.map((k) =>
      h('a', { href: wsHash(ws.name, k), class: tab === k ? 'active' : '' }, t('tab.' + k))));
    const body = { files: tabFiles, history: tabHistory, activity: tabActivity, settings: tabSettings, connect: tabConnect }[tab];
    return h('div', null, head, tabBar, await body(ws, route));
  }

  // ----------------------------------------------------------------- files
  function fsState(name) {
    if (!state.files[name]) state.files[name] = { entries: [], expanded: new Set(), dir: '', loaded: false };
    return state.files[name];
  }
  async function loadTree(name) {
    const fs = fsState(name);
    const data = await api('GET', wsApi(name) + '/tree?recursive=true');
    fs.entries = data.entries.filter((e) => !e.path.endsWith('/.gitkeep') && e.path !== '.gitkeep');
    fs.loaded = true;
    return fs;
  }

  async function tabFiles(ws, route) {
    const fs = fsState(ws.name);
    if (!fs.loaded) await loadTree(ws.name);
    const file = route.file || '';
    if (file) {
      const parts = file.split('/');
      for (let i = 1; i < parts.length; i++) fs.expanded.add(parts.slice(0, i).join('/'));
    }
    const treeEl = h('div', { class: 'tree' });
    const editorEl = h('div', { class: 'editor-panel' });
    const uploadInput = h('input', { type: 'file', multiple: true, class: 'hidden', onchange: () => doUpload(ws, uploadInput) });
    const dirBar = h('div', { class: 'panel-bar small' });

    const drawTree = () => {
      const children = new Map([['', []]]);
      for (const e of fs.entries) {
        const parent = e.path.includes('/') ? e.path.slice(0, e.path.lastIndexOf('/')) : '';
        if (!children.has(parent)) children.set(parent, []);
        children.get(parent).push(e);
      }
      const rows = [];
      const walk = (dir, depth) => {
        for (const e of children.get(dir) || []) {
          const isDir = e.type === 'dir';
          const open = fs.expanded.has(e.path);
          const sel = isDir ? fs.dir === e.path : file === e.path;
          const label = e.path.split('/').pop();
          rows.push(h('div', {
            class: 'node' + (isDir ? ' dir' : '') + (sel ? ' sel' : ''), style: 'padding-left:' + (10 + depth * 16) + 'px',
            title: e.path + (isDir ? '' : ' · ' + fmtBytes(e.size)),
            onclick: () => {
              if (isDir) {
                if (open && fs.dir === e.path) fs.expanded.delete(e.path); else fs.expanded.add(e.path);
                fs.dir = e.path; drawTree(); drawDirBar();
              } else { go(fileHash(ws.name, e.path)); }
            },
          }, h('span', { class: 'caret' }, isDir ? (open ? '▾' : '▸') : ''), h('span', { class: 'icon' }, isDir ? '📁' : fileIcon(e.path)),
          h('span', { class: 'label' }, label)));
          if (isDir && open) walk(e.path, depth + 1);
        }
      };
      walk('', 0);
      treeEl.replaceChildren(...(rows.length ? rows : [h('div', { class: 'placeholder' }, t('common.empty'))]));
    };
    const drawDirBar = () => {
      if (!fs.dir) { dirBar.classList.add('hidden'); return; }
      dirBar.classList.remove('hidden');
      dirBar.replaceChildren(h('span', { class: 'grow mono', style: 'overflow:hidden;text-overflow:ellipsis' }, '📁 ' + fs.dir + '/'),
        h('button', { class: 'btn sm ghost', title: t('files.move'), onclick: () => movePath(ws, fs.dir, true) }, '✎'),
        h('button', { class: 'btn sm ghost', title: t('common.delete'), onclick: () => deletePath(ws, fs.dir, true) }, '🗑'),
        h('button', { class: 'btn sm ghost', title: t('common.close'), onclick: () => { fs.dir = ''; drawTree(); drawDirBar(); } }, '✕'));
    };
    drawTree(); drawDirBar();

    const treePanel = h('div', { class: 'tree-panel' },
      h('div', { class: 'panel-bar' },
        h('button', { class: 'btn sm', onclick: () => newFile(ws, fs) }, '+ ' + t('files.newFile')),
        h('button', { class: 'btn sm', onclick: () => newFolder(ws, fs) }, '+ ' + t('files.newFolder')),
        h('button', { class: 'btn sm', onclick: () => uploadInput.click() }, '⇪ ' + t('files.upload')), uploadInput,
        h('div', { class: 'grow' }),
        h('button', { class: 'btn sm ghost', title: t('common.refresh'), onclick: async () => { await loadTree(ws.name); drawTree(); } }, '⟳'),
        h('a', { class: 'btn sm ghost', title: t('files.zip'), href: wsApi(ws.name) + '/archive' }, '⤓')),
      dirBar, treeEl);

    if (file) await openFile(ws, file, editorEl);
    else { state.editor = null; editorEl.replaceChildren(h('div', { class: 'placeholder' }, t('files.selectFile'))); }
    return h('div', { class: 'files' }, treePanel, editorEl);
  }

  function fileIcon(p) {
    const ext = (p.split('.').pop() || '').toLowerCase();
    if (['md', 'txt', 'rst'].includes(ext)) return '📝';
    if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'].includes(ext)) return '🖼';
    if (['json', 'yaml', 'yml', 'toml', 'csv', 'tsv'].includes(ext)) return '📊';
    if (['py', 'js', 'ts', 'go', 'rs', 'java', 'c', 'cpp', 'sh', 'html', 'css', 'tex'].includes(ext)) return '⌨';
    if (ext === 'pdf') return '📕';
    return '📄';
  }
  const isImage = (p) => /\.(png|jpe?g|gif|webp|bmp)$/i.test(p);

  async function openFile(ws, path, editorEl) {
    let data;
    try { data = await api('GET', wsApi(ws.name) + '/file?path=' + enc(path)); }
    catch (e) {
      state.editor = null;
      editorEl.replaceChildren(h('div', { class: 'placeholder' }, e.message));
      return;
    }
    state.editor = { ws: ws.name, path: data.path, version: data.version, original: data.encoding === 'utf-8' ? data.content : null,
      encoding: data.encoding, size: data.size };
    const ed = state.editor;
    const saveBtn = h('button', { class: 'btn primary sm', disabled: true, onclick: () => saveFile(ws, editorEl) }, t('common.save'));
    const dirty = h('span', { class: 'row small hidden', style: 'color:var(--warn)' }, h('span', { class: 'dirty-dot' }), t('files.unsaved'));
    const verBadge = h('span', { class: 'badge', title: 'version' }, t('files.version', { v: ed.version.slice(0, 8) }));
    const headEl = h('div', { class: 'editor-head' },
      h('span', { class: 'path grow' }, ed.path), verBadge, h('span', { class: 'faint small' }, fmtBytes(ed.size)), dirty,
      h('button', { class: 'btn sm ghost', onclick: () => fileHistory(ws, ed.path, editorEl) }, '🕘 ' + t('files.fileHistory')),
      h('button', { class: 'btn sm ghost', onclick: () => movePath(ws, ed.path, false) }, '✎ ' + t('files.move')),
      h('a', { class: 'btn sm ghost', href: wsApi(ws.name) + '/raw/' + encPath(ed.path) + '?download=true' }, '⤓'),
      h('button', { class: 'btn sm ghost', title: t('common.delete'), onclick: () => deletePath(ws, ed.path, false) }, '🗑'),
      ed.encoding === 'utf-8' ? saveBtn : null);
    let bodyEl;
    if (ed.encoding === 'utf-8') {
      const ta = h('textarea', { id: 'editor-text', spellcheck: 'false', value: data.content });
      ta.addEventListener('input', () => {
        const d = ta.value !== ed.original;
        saveBtn.disabled = !d; dirty.classList.toggle('hidden', !d);
      });
      ta.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); if (!saveBtn.disabled) saveFile(ws, editorEl); }
      });
      bodyEl = h('div', { class: 'editor' }, ta);
    } else if (isImage(ed.path)) {
      bodyEl = h('div', { class: 'editor' }, h('img', { class: 'preview-img', alt: ed.path, src: wsApi(ws.name) + '/raw/' + encPath(ed.path) + '?v=' + ed.version }));
    } else {
      bodyEl = h('div', { class: 'placeholder' }, t('files.binary', { size: fmtBytes(ed.size) }));
    }
    editorEl.replaceChildren(headEl, bodyEl);
  }

  async function saveFile(ws, editorEl, force) {
    const ed = state.editor;
    const ta = document.getElementById('editor-text');
    if (!ed || !ta) return;
    const content = ta.value;
    try {
      const r = await api('PUT', wsApi(ws.name) + '/file', { path: ed.path, content, expected_version: force ? null : ed.version });
      ed.version = r.version; ed.original = content; ed.size = r.size;
      toast(t('files.saved'));
      await loadTree(ws.name);
      const keep = ta.value;
      await openFile(ws, ed.path, editorEl);
      const nta = document.getElementById('editor-text');
      if (nta && nta.value !== keep) nta.value = keep;
    } catch (e) {
      if (e.status === 409) conflictDialog(ws, editorEl);
      else toast(e.message, true);
    }
  }

  function conflictDialog(ws, editorEl) {
    const m = modal({
      title: t('common.error'), body: h('p', null, t('files.conflict')),
      foot: [
        h('button', { class: 'btn', onclick: async () => { m.close(); await openFile(ws, state.editor.path, editorEl); } }, t('files.conflictReload')),
        h('button', { class: 'btn danger solid', onclick: () => { m.close(); saveFile(ws, editorEl, true); } }, t('files.conflictOverwrite'))],
    });
  }

  async function newFile(ws, fs) {
    const path = await promptDialog(t('files.newFile'), t('files.pathPrompt'), fs.dir ? fs.dir + '/' : 'files/', null, t('common.create'));
    if (!path) return;
    try {
      await api('PUT', wsApi(ws.name) + '/file', { path, content: '', expected_version: 'new' });
      await loadTree(ws.name);
      go(fileHash(ws.name, path.replace(/^\/+/, '')));
    } catch (e) { toast(e.message, true); }
  }

  async function newFolder(ws, fs) {
    const path = await promptDialog(t('files.newFolder'), t('files.folderPrompt'), fs.dir ? fs.dir + '/' : '', null, t('common.create'));
    if (!path) return;
    try {
      const r = await api('POST', wsApi(ws.name) + '/mkdir', { path });
      await loadTree(ws.name);
      fs.dir = r.path; fs.expanded.add(r.path);
      render();
    } catch (e) { toast(e.message, true); }
  }

  async function doUpload(ws, input) {
    if (!input.files.length) return;
    const fs = fsState(ws.name);
    const form = new FormData();
    form.append('dir', fs.dir || (state.editor && state.editor.path.includes('/') ? state.editor.path.slice(0, state.editor.path.lastIndexOf('/')) : 'files'));
    for (const f of input.files) form.append('files', f, f.name);
    try {
      const r = await api('POST', wsApi(ws.name) + '/upload', undefined, { form });
      toast(t('files.uploaded', { n: r.uploaded.length }));
      await loadTree(ws.name);
      render();
    } catch (e) { toast(e.message, true); }
    input.value = '';
  }

  async function movePath(ws, path, isDir) {
    const dest = await promptDialog(t('files.move'), t('files.moveTo'), path, null, t('common.save'));
    if (!dest || dest === path) return;
    try {
      const r = await api('POST', wsApi(ws.name) + '/move', { source: path, destination: dest });
      await loadTree(ws.name);
      const fs = fsState(ws.name);
      if (isDir) { fs.dir = r.destination; fs.expanded.add(r.destination); render(); }
      else { state.editor = null; go(fileHash(ws.name, r.destination)); }
    } catch (e) { toast(e.message, true); }
  }

  async function deletePath(ws, path, isDir) {
    if (!(await confirmDialog(t('files.deleteConfirm', { path }), t('common.delete'), true))) return;
    try {
      await api('DELETE', wsApi(ws.name) + '/file?path=' + enc(path) + '&recursive=' + (isDir ? 'true' : 'false'));
      await loadTree(ws.name);
      const fs = fsState(ws.name);
      if (isDir) fs.dir = '';
      state.editor = null;
      go(wsHash(ws.name));
    } catch (e) { toast(e.message, true); }
  }

  async function fileHistory(ws, path, editorEl) {
    const data = await api('GET', wsApi(ws.name) + '/history?path=' + enc(path) + '&limit=50');
    const viewer = h('div');
    const list = h('div', { class: 'table-wrap' });
    if (!data.commits.length) list.append(h('div', { class: 'placeholder' }, t('files.noHistory')));
    for (const c of data.commits) {
      list.append(h('div', { class: 'commit', style: 'cursor:pointer', onclick: () => showRevision(c) },
        avatar(c.author),
        h('div', { class: 'grow' }, h('div', { class: 'msg' }, c.message),
          h('div', { class: 'small muted' }, c.author, ' · ', timeEl(c.time), ' · ', h('span', { class: 'mono' }, c.short)))));
    }
    const showRevision = async (c) => {
      try {
        const r = await api('GET', wsApi(ws.name) + '/file?path=' + enc(path) + '&revision=' + enc(c.revision));
        const restore = h('button', { class: 'btn primary sm', onclick: async () => {
          try {
            await api('PUT', wsApi(ws.name) + '/file', { path, content: r.content, encoding: r.encoding, message: 'Restore ' + path + ' to ' + c.short });
            toast(t('files.restored')); m.close(); await loadTree(ws.name); await openFile(ws, path, editorEl);
          } catch (e) { toast(e.message, true); }
        } }, t('files.restoreVersion'));
        viewer.replaceChildren(h('div', { class: 'banner info' }, h('span', { class: 'grow' }, t('files.viewing', { rev: c.short + ' · ' + c.author })), restore),
          r.encoding === 'utf-8' ? h('textarea', { readonly: true, rows: 16, class: 'mono', style: 'margin-top:10px', value: r.content })
            : h('p', { class: 'muted' }, t('files.binary', { size: fmtBytes(r.size) })));
      } catch (e) { toast(e.message, true); }
    };
    const m = modal({ title: t('files.fileHistory') + ' · ' + path, wide: true, body: [list, viewer] });
  }

  function avatar(name) {
    return h('div', { class: 'avatar', style: 'background:' + colorFor(name) }, (String(name || '?').trim()[0] || '?').toUpperCase());
  }

  // --------------------------------------------------------------- history
  async function tabHistory(ws) {
    const data = await api('GET', wsApi(ws.name) + '/history?limit=100');
    if (!data.commits.length) return h('div', { class: 'empty' }, t('history.empty'));
    return h('div', { class: 'table-wrap' }, data.commits.map((c) =>
      h('div', { class: 'commit' }, avatar(c.author),
        h('div', { class: 'grow' }, h('div', { class: 'msg' }, c.message),
          h('div', { class: 'small muted' }, c.author, ' · ', timeEl(c.time), ' · ', h('span', { class: 'mono' }, c.short)),
          c.files.length ? h('div', { class: 'chips' }, c.files.slice(0, 20).map((f) =>
            h(f.status === 'D' ? 'span' : 'a', { class: 'chip', href: f.status === 'D' ? null : fileHash(ws.name, f.path) },
              h('span', { class: 'st st-' + f.status }, f.status), f.path))) : null))));
  }

  // -------------------------------------------------------------- activity
  function activityTable(rows, showWs) {
    if (!rows.length) return h('div', { class: 'empty' }, t('common.empty'));
    return h('div', { class: 'table-wrap' }, h('table', null,
      h('thead', null, h('tr', null, h('th', null, t('common.time')), h('th', null, t('common.who')), h('th', null, t('common.what')),
        showWs ? h('th', null, t('common.workspace')) : null, h('th', null, t('common.path')))),
      h('tbody', null, rows.map((a) => h('tr', null,
        h('td', { style: 'white-space:nowrap' }, timeEl(a.time)), h('td', null, a.actor),
        h('td', null, h('span', { class: 'badge' }, a.action), a.detail ? h('div', { class: 'small faint', style: 'overflow-wrap:anywhere' }, a.detail) : null),
        showWs ? h('td', null, a.workspace ? h('a', { href: wsHash(a.workspace) }, a.workspace) : '—') : null,
        h('td', { class: 'mono' }, a.path && a.workspace && a.action.startsWith('file.') && a.action !== 'file.delete'
          ? h('a', { href: fileHash(a.workspace, a.path) }, a.path) : (a.path || '')))))));
  }

  async function tabActivity(ws) {
    const data = await api('GET', '/api/activity?limit=200&workspace=' + enc(ws.name));
    return activityTable(data.activity, false);
  }

  async function pageActivity() {
    const [data, wss] = await Promise.all([api('GET', '/api/activity?limit=300'), api('GET', '/api/workspaces')]);
    const sel = h('select', { style: 'max-width:240px' }, h('option', { value: '' }, t('activity.filter')),
      wss.workspaces.map((w) => h('option', { value: w.name }, w.name)));
    const holder = h('div', null, activityTable(data.activity, true));
    sel.addEventListener('change', async () => {
      const d = await api('GET', '/api/activity?limit=300' + (sel.value ? '&workspace=' + enc(sel.value) : ''));
      holder.replaceChildren(activityTable(d.activity, true));
    });
    return h('div', null, h('div', { class: 'page-head' }, h('h1', { class: 'grow' }, t('activity.title')), sel), holder);
  }

  // -------------------------------------------------------------- settings
  async function tabSettings(ws) {
    const desc = h('textarea', { rows: 4, value: ws.description || '' });
    const pw = h('input', { type: 'text', autocomplete: 'off' });
    const newName = h('input', { type: 'text', value: ws.name });
    const confirmName = h('input', { type: 'text', autocomplete: 'off', placeholder: ws.name });
    const patch = async (body, okMsg) => {
      try {
        const r = await api('PATCH', wsApi(ws.name), body);
        toast(okMsg || t('settings.updated'));
        if (body.new_name) { delete state.files[ws.name]; go(wsHash(r.name, 'settings')); } else render();
      } catch (e) { toast(e.message, true); }
    };
    const delBtn = h('button', { class: 'btn danger solid', disabled: true, onclick: async () => {
      try { await api('DELETE', wsApi(ws.name)); toast(t('settings.deleted')); delete state.files[ws.name]; go('#/'); }
      catch (e) { toast(e.message, true); }
    } }, t('settings.deleteBtn'));
    confirmName.addEventListener('input', () => { delBtn.disabled = confirmName.value !== ws.name; });
    return h('div', { style: 'max-width:760px' },
      h('div', { class: 'card stack' }, h('h2', null, t('settings.description')), desc,
        h('div', null, h('button', { class: 'btn primary', onclick: () => patch({ description: desc.value }) }, t('common.save')))),
      h('div', { class: 'card stack' }, h('h2', null, t('settings.password')),
        h('p', { class: 'muted', style: 'margin:0' }, ws.has_password ? t('settings.passwordSet') : t('settings.passwordUnset')),
        h('label', { class: 'field' }, h('span', null, t('settings.newPassword')),
          h('div', { class: 'row' }, pw, h('button', { class: 'btn', onclick: () => { pw.value = genPassword(); } }, t('common.generate')))),
        h('div', { class: 'row' },
          h('button', { class: 'btn primary', onclick: () => { if (pw.value) patch({ password: pw.value }); } }, t('settings.setPassword')),
          ws.has_password ? h('button', { class: 'btn', onclick: () => patch({ clear_password: true }) }, t('settings.removePassword')) : null)),
      h('div', { class: 'card stack' }, h('h2', null, t('settings.rename')),
        h('label', { class: 'field' }, h('span', null, t('settings.newName')), newName, h('div', { class: 'hint' }, t('ws.nameHint'))),
        h('div', null, h('button', { class: 'btn', onclick: () => { if (newName.value.trim() && newName.value.trim() !== ws.name) patch({ new_name: newName.value.trim() }); } }, t('settings.renameBtn')))),
      h('div', { class: 'card stack', style: 'border-color:var(--danger)' }, h('h2', { style: 'color:var(--danger)' }, t('settings.danger')),
        h('p', { class: 'muted', style: 'margin:0' }, t('settings.deleteText')),
        h('label', { class: 'field' }, h('span', null, t('settings.deleteConfirm')), confirmName), h('div', null, delBtn)));
  }

  // --------------------------------------------------------------- connect
  function connectGuide(wsName) {
    const base = (state.info && state.info.base_url) || location.origin;
    const mcp = base + '/mcp';
    const wsLabel = wsName || '<workspace>';
    const user = wsName || 'agent';
    const envSnippet = 'export CAW_URL="' + base + '"\nexport CAW_USER="' + user + '"\nexport CAW_PASSWORD="<' + (wsName ? 'workspace' : 'agent') + ' password>"\n'
      + 'python3 caw.py start ' + (wsName || '<workspace>');
    const curlSnippet = 'curl -u "' + user + ':$CAW_PASSWORD" "' + base + '/api/workspaces/' + enc(wsLabel) + '/session?format=md"';
    const tokenBtn = h('button', { class: 'btn', onclick: () => createTokenDialog(wsName) }, '+ ' + t('connect.createToken'));
    return h('div', { class: 'stack', style: 'max-width:900px' },
      h('p', { class: 'muted', style: 'margin-top:0' }, t('connect.intro')),
      h('div', { class: 'card stack' }, h('h3', null, t('connect.mcpUrl')), copyBox(mcp)),
      h('div', { class: 'card stack' }, h('h3', null, t('connect.oauthTitle')), h('p', { class: 'muted', style: 'margin:0' }, t('connect.oauthText')),
        h('div', null, h('b', null, t('connect.platforms'))),
        h('ul', { class: 'steps' }, h('li', null, t('connect.claude')), h('li', null, t('connect.chatgpt')), h('li', null, t('connect.gemini')),
          h('li', null, t('connect.cli', { url: mcp })))),
      h('div', { class: 'card stack' }, h('h3', null, t('connect.tokenTitle')), h('p', { class: 'muted', style: 'margin:0' }, t('connect.tokenText')),
        h('div', null, tokenBtn)),
      h('div', { class: 'card stack' }, h('h3', null, t('connect.headerTitle')),
        copyBox('Authorization: Basic base64("' + user + ':<password>")\nAuthorization: Bearer caw_…   (token)\nX-CAW-Agent: Claude Code   (optional author name)', true)),
      h('div', { class: 'card stack' }, h('h3', null, t('connect.skillTitle')), h('p', { class: 'muted', style: 'margin:0' }, t('connect.skillText')),
        copyBox(envSnippet, true), copyBox(curlSnippet, true)),
      h('div', { class: 'card stack' }, h('h3', null, t('connect.promptTitle')), copyBox(t('connect.prompt', { ws: wsLabel }), true)),
      h('p', null, h('a', { href: 'https://github.com/conscienceli/cross-agent-workspace#readme', target: '_blank', rel: 'noopener' }, t('connect.docs') + ' ↗')));
  }
  async function tabConnect(ws) { return connectGuide(ws.name); }
  async function pageConnect() {
    return h('div', null, h('div', { class: 'page-head' }, h('h1', null, t('connect.title'))), connectGuide(null));
  }

  // ---------------------------------------------------------------- tokens
  async function createTokenDialog(wsName, onDone) {
    const wss = (await api('GET', '/api/workspaces')).workspaces;
    const name = h('input', { type: 'text', value: wsName ? wsName + '-agent' : '' });
    const role = h('select', null, h('option', { value: 'workspace' }, t('role.workspace')), h('option', { value: 'agent' }, t('role.agent')));
    const wsSel = h('select', null, wss.map((w) => h('option', { value: w.name }, w.name)));
    if (wsName) wsSel.value = wsName;
    const exp = h('input', { type: 'number', min: 1, placeholder: t('common.never') });
    const wsField = h('label', { class: 'field' }, h('span', null, t('common.workspace')), wsSel);
    role.addEventListener('change', () => wsField.classList.toggle('hidden', role.value !== 'workspace'));
    const err = h('div', { class: 'err-text' });
    const submit = async () => {
      err.textContent = '';
      try {
        const body = { name: name.value.trim() || 'agent', role: role.value, workspace: role.value === 'workspace' ? wsSel.value : null,
          expires_days: exp.value ? parseInt(exp.value, 10) : null };
        const r = await api('POST', '/api/tokens', body);
        m.close(); tokenResult(r);
        if (onDone) onDone();
      } catch (e) { err.textContent = e.message; }
    };
    const m = modal({
      title: t('tokens.new'),
      body: [h('label', { class: 'field' }, h('span', null, t('common.name')), name, h('div', { class: 'hint' }, t('tokens.nameHint'))),
        h('label', { class: 'field' }, h('span', null, t('common.role')), role), wsField,
        h('label', { class: 'field' }, h('span', null, t('tokens.expiresDays'), ' ', h('span', { class: 'faint' }, '(' + t('common.optional') + ')')), exp), err],
      foot: [h('button', { class: 'btn', onclick: () => m.close() }, t('common.cancel')), h('button', { class: 'btn primary', onclick: submit }, t('common.create'))],
    });
  }

  function tokenResult(r) {
    modal({
      title: t('tokens.new') + ' · ' + r.name, wide: true,
      body: [h('div', { class: 'banner warn' }, t('tokens.created')),
        h('div', null, h('b', null, 'Token'), copyBox(r.token)),
        h('div', null, h('b', null, t('tokens.urlWithToken')), copyBox(r.mcp_url_with_token)),
        h('div', null, h('b', null, t('tokens.header')), copyBox('Authorization: Bearer ' + r.token))],
    });
  }

  async function pageTokens() {
    const data = await api('GET', '/api/tokens');
    const rows = data.tokens.map((tk) => h('tr', null,
      h('td', null, h('b', null, tk.name), h('div', { class: 'mono faint' }, tk.prefix + '…')),
      h('td', null, h('span', { class: 'badge accent' }, t('role.' + tk.role))),
      h('td', null, tk.workspace ? h('a', { href: wsHash(tk.workspace) }, tk.workspace) : '—'),
      h('td', null, timeEl(tk.created_at)), h('td', null, tk.last_used_at ? timeEl(tk.last_used_at) : t('common.never')),
      h('td', null, tk.expires_at ? absTime(tk.expires_at) : t('common.never')),
      h('td', null, h('button', { class: 'btn danger sm', onclick: async () => {
        if (!(await confirmDialog(t('tokens.revokeConfirm', { name: tk.name }), t('common.revoke'), true))) return;
        try { await api('DELETE', '/api/tokens/' + enc(tk.id)); render(); } catch (e) { toast(e.message, true); }
      } }, t('common.revoke')))));
    return h('div', null,
      h('div', { class: 'page-head' }, h('h1', { class: 'grow' }, t('tokens.title')),
        h('button', { class: 'btn primary', onclick: () => createTokenDialog(null, render) }, '+ ' + t('tokens.new'))),
      h('p', { class: 'muted' }, t('tokens.intro')),
      rows.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, [t('common.name'), t('common.role'), t('common.workspace'), t('common.created'), t('common.lastUsed'), t('common.expires'), ''].map((x) => h('th', null, x)))),
        h('tbody', null, rows))) : h('div', { class: 'empty' }, t('common.empty')));
  }

  // ----------------------------------------------------------- connections
  async function pageConnections() {
    const data = await api('GET', '/api/connections');
    const rows = data.connections.map((c) => h('tr', null,
      h('td', null, h('b', null, c.client_name), h('div', { class: 'small faint mono' }, c.client_id.length > 48 ? c.client_id.slice(0, 48) + '…' : c.client_id)),
      h('td', null, h('span', { class: 'badge accent' }, t('role.' + c.role))),
      h('td', null, c.workspace ? h('a', { href: wsHash(c.workspace) }, c.workspace) : t('common.all')),
      h('td', null, timeEl(c.created_at)), h('td', null, c.last_used_at ? timeEl(c.last_used_at) : '—'),
      h('td', null, h('button', { class: 'btn danger sm', onclick: async () => {
        if (!(await confirmDialog(t('conn.revokeConfirm', { name: c.client_name }), t('common.revoke'), true))) return;
        try { await api('DELETE', '/api/connections/' + enc(c.id)); render(); } catch (e) { toast(e.message, true); }
      } }, t('common.revoke')))));
    return h('div', null, h('div', { class: 'page-head' }, h('h1', null, t('conn.title'))),
      h('p', { class: 'muted' }, t('conn.intro')),
      rows.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, [t('conn.app'), t('common.role'), t('common.workspace'), t('common.created'), t('common.lastUsed'), ''].map((x) => h('th', null, x)))),
        h('tbody', null, rows))) : h('div', { class: 'empty' }, t('common.empty')));
  }

  // ----------------------------------------------------------------- trash
  async function pageTrash() {
    const data = await api('GET', '/api/trash');
    const rows = data.trash.map((x) => h('tr', null,
      h('td', null, h('b', null, x.name), h('div', { class: 'small faint mono' }, x.trash_id)),
      h('td', null, x.description || '—'), h('td', null, timeEl(x.deleted_at), x.deleted_by ? h('div', { class: 'small faint' }, x.deleted_by) : null),
      h('td', { style: 'white-space:nowrap' },
        h('button', { class: 'btn sm', onclick: async () => {
          const name = await promptDialog(t('common.restore'), t('trash.restoreName'), x.name, t('ws.nameHint'), t('common.restore'));
          if (!name) return;
          try { const r = await api('POST', '/api/trash/' + enc(x.trash_id) + '/restore', { name }); toast(t('trash.restored')); go(wsHash(r.name)); }
          catch (e) { toast(e.message, true); }
        } }, t('common.restore')), ' ',
        h('button', { class: 'btn danger sm', onclick: async () => {
          if (!(await confirmDialog(t('trash.purgeConfirm', { name: x.name }), t('trash.purge'), true))) return;
          try { await api('DELETE', '/api/trash/' + enc(x.trash_id)); render(); } catch (e) { toast(e.message, true); }
        } }, t('trash.purge')))));
    return h('div', null, h('div', { class: 'page-head' }, h('h1', null, t('trash.title'))),
      h('p', { class: 'muted' }, t('trash.intro')),
      rows.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, [t('common.name'), t('common.description'), t('trash.deleted'), ''].map((x) => h('th', null, x)))),
        h('tbody', null, rows))) : h('div', { class: 'empty' }, t('common.empty')));
  }

  initI18n().then(render);
})();
