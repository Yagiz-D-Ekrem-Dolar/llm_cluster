'use strict';
/* Panel çekirdeği: yardımcılar, API, bileşenler, canlı grafikler, yönlendirici */

const COLORS = { blue: '#0078d7', green: '#107c10', orange: '#d83b01', purple: '#8764b8', red: '#c42b1c', teal: '#008272', yellow: '#f2b100', gray: '#605e5c' };
// Görev Yöneticisi'ndeki gibi her metriğin kendi rengi
const GC = { cpu: '#117dbb', mem: '#8b12ae', gpu: '#4da613', vram: '#2d7d9a', temp: '#c8571a', pw: '#a15814', net: '#5c7e99', tps: '#0078d7', req: '#0078d7' };

// ------------------------------------------------------------------ yardımcılar
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));

const fmt = {
  n(v, d = 0) { return v == null || isNaN(v) ? '—' : Number(v).toLocaleString('tr-TR', { minimumFractionDigits: d, maximumFractionDigits: d }); },
  pct(v) { return v == null ? '—' : Math.round(v) + '%'; },
  gb(v) { return v == null ? '—' : (v >= 100 ? Math.round(v) : Number(v).toFixed(1).replace('.', ',')) + ' GB'; },
  temp(v) { return v == null ? '—' : Math.round(v) + '°C'; },
  w(v) { return v == null ? '—' : Math.round(v) + ' W'; },
  ms(v) { return !v ? '—' : v >= 1000 ? (v / 1000).toFixed(1).replace('.', ',') + ' sn' : Math.round(v) + ' ms'; },
  dur(s) {
    if (s == null) return '—';
    s = Math.floor(s);
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d} g ${h} sa` : h ? `${h} sa ${m} dk` : m ? `${m} dk` : `${s} sn`;
  },
  ago(ts) { if (!ts) return '—'; const s = Date.now() / 1000 - ts; return s < 5 ? 'şimdi' : fmt.dur(s) + ' önce'; },
  time(ts) { return new Date(ts * 1000).toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }); },
  hm(ts) { return new Date(ts * 1000).toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' }); },
  dt(ts) { return ts ? new Date(ts * 1000).toLocaleString('tr-TR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'; },
  size(b) { if (b == null) return '—'; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0, v = +b; while (v >= 1024 && i < 4) { v /= 1024; i++; } return (i ? v.toFixed(v >= 100 ? 0 : 1).replace('.', ',') : v) + ' ' + u[i]; },
  bps(v) { if (v == null) return '—'; if (v > 1e6) return (v / 1e6).toFixed(1).replace('.', ',') + ' MB/sn'; if (v > 1e3) return Math.round(v / 1e3) + ' KB/sn'; return Math.round(v) + ' B/sn'; },
};

const ICONS = {
  grid: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>',
  server: '<rect x="3" y="4" width="18" height="7"/><rect x="3" y="13" width="18" height="7"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  layers: '<path d="M12 3 3 8l9 5 9-5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  chat: '<path d="M4 4h16v12H9l-5 4V4z"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18.5 14.5A6.5 6.5 0 0 1 21.5 20"/>',
  sliders: '<path d="M4 6h8M18 6h2M4 12h2M12 12h8M4 18h10M20 18h0"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  flask: '<path d="M9 3h6M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3"/><path d="M7.5 15h9"/>',
  cloud: '<path d="M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 9.5 4 4 0 0 1 17.5 18H7z"/><path d="M12 12v6M9.5 14.5 12 12l2.5 2.5"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4 6.5 6.5 0 0 0 20 14.5z"/>',
  power: '<path d="M12 3v9"/><path d="M6.3 6.6a8 8 0 1 0 11.4 0"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14-4.5L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14 4.5L20 16"/><path d="M20 20v-4h-4"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16v4z"/><path d="m13.5 6.5 4 4"/>',
  play: '<path d="M7 4.5v15l12-7.5-12-7.5z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  stop: '<rect x="6" y="6" width="12" height="12"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  copy: '<rect x="8" y="8" width="12" height="12"/><path d="M16 8V4H4v12h4"/>',
  download: '<path d="M12 4v11M7.5 11 12 15.5 16.5 11M5 20h14"/>',
  upload: '<path d="M12 16V5M7.5 9 12 4.5 16.5 9M5 20h14"/>',
  alert: '<path d="M12 3.5 2.5 20h19L12 3.5z"/><path d="M12 10v4.5M12 17.5h.01"/>',
  more: '<circle cx="5" cy="12" r="1.3" fill="currentColor"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="19" cy="12" r="1.3" fill="currentColor"/>',
  logout: '<path d="M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4"/><path d="m16 8 4 4-4 4M20 12H9"/>',
  send: '<path d="M4 12 20 4l-4 16-4-6.5L4 12z"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M16 7l3 3"/>',
  shuffle: '<path d="M3 7h4l10 10h4M3 17h4l3-3M14 10l3-3h4M18 4l3 3-3 3M18 14l3 3-3 3"/>',
  list: '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  network: '<rect x="9" y="3" width="6" height="5"/><rect x="3" y="16" width="6" height="5"/><rect x="15" y="16" width="6" height="5"/><path d="M12 8v4M6 16v-4h12v4"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
  box: '<path d="M3 8 12 3l9 5v8l-9 5-9-5V8z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
  speaker: '<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/>',
};
const icon = (n, cls = '') => `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">${ICONS[n] || ''}</svg>`;

const NODE_ST = { online: ['Çevrimiçi', 'ok'], sleeping: ['Uykuda', 'idle'], offline: ['Çevrimdışı', 'err'], waking: ['Uyanıyor', 'warn'], rebooting: ['Yeniden başlıyor', 'warn'], unknown: ['Bilinmiyor', 'idle'] };
const DEP_ST = { ready: ['Hazır', 'ok'], starting: ['Yükleniyor', 'warn'], syncing: ['Dosyalar geliyor', 'info'], pending: ['Bekliyor', 'info'], failed: ['Hata', 'err'], stopped: ['Durduruldu', 'idle'] };
const badge = (text, cls = 'idle') => `<span class="badge b-${cls}"><i></i>${esc(text)}</span>`;
const sq = cls => `<span class="sq ${cls || ''}"></span>`;
function nodeState(n) {
  if (!n.enabled) return ['Bakımda', 'idle'];
  if (n.status !== 'online') return NODE_ST[n.status] || ['?', 'idle'];
  if (n.thermal === 'crit') return ['Aşırı ısındı', 'err'];
  if (n.draining) return ['Boşaltılıyor', 'warn'];
  if (n.health === 'crit') return ['Kritik', 'err'];
  if (n.health === 'warn' || n.thermal === 'warn') return ['Uyarı', 'warn'];
  return ['Çevrimiçi', 'ok'];
}
const nodeBadge = n => { const [t, c] = nodeState(n); return badge(t, c); };
const depLabel = d => d.state === 'syncing' && d.sync ? d.sync.step + (d.sync.pct != null ? ` %${Math.round(d.sync.pct)}` : '') : (DEP_ST[d.state] || ['?'])[0];
const depBadge = d => { const [, c] = DEP_ST[d.state] || ['?', 'idle']; return d.desired === 'stopped' ? badge('Durduruldu', 'idle') : badge(depLabel(d), c); };
const bar = (p, cls = '') => `<div class="bar ${cls}"><i style="width:${clamp((p || 0) / 100) * 100}%"></i></div>`;
const loadCls = p => p >= 90 ? 'err' : p >= 75 ? 'warn' : '';
const heat = p => p == null ? '' : p >= 80 ? 'h4' : p >= 60 ? 'h3' : p >= 30 ? 'h2' : p >= 10 ? 'h1' : '';
const tempCls = (t, n) => t == null ? '' : t >= (n?.crit_c ?? 90) ? 't-err' : t >= (n?.warn_c ?? 80) ? 't-warn' : 't-ok';
const chips = (arr, cls = '') => (arr || []).map(x => `<span class="chip ${cls}">${esc(x)}</span>`).join(' ');

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const ta = document.createElement('textarea'); ta.value = text; ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); } catch (e2) { /* yoksay */ }
    ta.remove();
  }
  toast('Panoya kopyalandı', 'ok', 1800);
}

// ------------------------------------------------------------------ API
async function api(path, { method = 'GET', body, raw = false, noAuth = false, signal } = {}) {
  const headers = { 'X-Orkestra': '1' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const r = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, credentials: 'same-origin', signal });
  if (raw) return r;
  if (r.status === 401 && !noAuth) { App.showAuth('login'); throw new Error('Oturum gerekli'); }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) {
    const msg = typeof data === 'object' && data ? (typeof data.detail === 'string' ? data.detail : (data.error?.message || JSON.stringify(data.detail || data))) : (data || r.statusText);
    throw new Error(msg);
  }
  return data;
}
const post = (p, body = {}) => api(p, { method: 'POST', body });
const put = (p, body = {}) => api(p, { method: 'PUT', body });
const patch = (p, body = {}) => api(p, { method: 'PATCH', body });
const del = p => api(p, { method: 'DELETE' });

// ------------------------------------------------------------------ bileşenler
function toast(msg, kind = 'info', ms = 4200) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = `<div class="grow">${esc(msg)}</div>`;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), ms);
}
const toastErr = e => toast(e && e.message ? e.message : String(e), 'err', 6000);

const modalStack = [];
function modal({ title = '', body = '', actions = [], size = '', drawer = false, onClose } = {}) {
  const bg = document.createElement('div');
  bg.className = 'bg' + (drawer ? ' drawer-bg' : '');
  bg.innerHTML = `<div class="${drawer ? 'drawer' : 'modal ' + size}" role="dialog" aria-modal="true">
    <div class="modal-h"><h3>${title}</h3><button class="btn ghost ic sm x" data-x aria-label="Kapat">${icon('x')}</button></div>
    <div class="modal-b"></div>${actions.length ? '<div class="modal-f"></div>' : ''}</div>`;
  const box = bg.firstElementChild, bodyEl = $('.modal-b', box);
  if (typeof body === 'string') bodyEl.innerHTML = body; else bodyEl.append(body);
  const m = { el: box, body: bodyEl, bg };
  m.close = () => { if (!bg.isConnected) return; bg.remove(); const i = modalStack.indexOf(m); if (i >= 0) modalStack.splice(i, 1); onClose && onClose(); };
  m.title = t => { $('.modal-h h3', box).innerHTML = t; };
  const foot = $('.modal-f', box);
  actions.forEach(a => {
    const b = document.createElement('button');
    b.className = 'btn ' + (a.kind || ''); b.innerHTML = (a.icon ? icon(a.icon) : '') + esc(a.label);
    b.addEventListener('click', async () => {
      b.disabled = true;
      try { const r = await a.onClick?.(m); if (r !== false && a.close !== false) m.close(); }
      catch (e) { toastErr(e); }
      finally { b.disabled = false; }
    });
    foot.appendChild(b);
  });
  bg.addEventListener('mousedown', e => { if (e.target === bg) m.close(); });
  $('[data-x]', box).addEventListener('click', m.close);
  document.body.appendChild(bg);
  modalStack.push(m);
  const first = $('input:not([type=hidden]),select,textarea', bodyEl); if (first && !drawer) setTimeout(() => first.focus(), 30);
  return m;
}
document.addEventListener('keydown', e => { if (e.key === 'Escape' && modalStack.length) modalStack[modalStack.length - 1].close(); });
const closeAllModals = () => [...modalStack].forEach(m => m.close());

function confirmBox(message, { title = 'Onay', ok = 'Tamam', danger = false } = {}) {
  return new Promise(res => {
    let done = false;
    modal({ title: esc(title), body: `<p style="margin:0;line-height:1.55">${message}</p>`,
      actions: [{ label: 'Vazgeç', onClick: () => { done = true; res(false); } }, { label: ok, kind: danger ? 'danger' : 'pri', onClick: () => { done = true; res(true); } }],
      onClose: () => { if (!done) res(false); } });
  });
}

const fld = (label, input, hint = '', cls = '') => `<label class="fld ${cls}"><span>${label}</span>${input}${hint ? `<small>${hint}</small>` : ''}</label>`;
const chk = (name, label, checked, hint = '') => `<label class="chk"><input type="checkbox" name="${name}" ${checked ? 'checked' : ''}><span>${label}${hint ? `<small>${hint}</small>` : ''}</span></label>`;
const opt = (v, t, sel) => `<option value="${esc(v)}" ${String(sel) === String(v) ? 'selected' : ''}>${esc(t)}</option>`;
const sw = (name, checked) => `<span class="sw"><input type="checkbox" name="${name}" ${checked ? 'checked' : ''}><i></i></span>`;
function formVals(root) {
  const out = {};
  $$('[name]', root).forEach(el => {
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else if (el.type === 'radio') { if (el.checked) out[el.name] = el.value; }
    else if (el.type === 'number') out[el.name] = el.value === '' ? null : Number(el.value);
    else out[el.name] = el.value;
  });
  return out;
}
function on(el, type, sel, fn) { el.addEventListener(type, e => { const t = e.target.closest(sel); if (t && el.contains(t)) fn(t, e); }); }

function popMenu(anchor, items) {
  $$('.menu.pop').forEach(m => m.remove());
  const m = document.createElement('div'); m.className = 'menu pop';
  m.innerHTML = items.map((it, i) => it === '-' ? '<hr>' : `<button data-i="${i}" class="${it.danger ? 'danger' : ''}" ${it.disabled ? 'disabled' : ''}>${it.icon ? icon(it.icon) : ''}${esc(it.label)}</button>`).join('');
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  m.style.position = 'fixed'; m.style.left = 'auto';
  m.style.right = Math.max(8, innerWidth - r.right) + 'px';
  m.style.top = Math.max(8, Math.min(innerHeight - m.offsetHeight - 8, r.bottom + 2)) + 'px';
  const close = () => { m.remove(); document.removeEventListener('mousedown', away, true); };
  const away = e => { if (!m.contains(e.target)) close(); };
  setTimeout(() => document.addEventListener('mousedown', away, true), 0);
  m.addEventListener('click', e => {
    const b = e.target.closest('button[data-i]'); if (!b) return;
    close(); const it = items[+b.dataset.i]; Promise.resolve(it.onClick && it.onClick()).catch(toastErr);
  });
}

// ------------------------------------------------------------------ canlı veri ve grafikler
function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v * 1.1)));
  for (const k of [1, 2, 2.5, 5, 10]) if (k * p >= v * 1.08) return k * p;
  return 10 * p;
}

/** Sunucudan gelen örnekleri (düğüm başına sütunlu diziler) tutar; grafikler buradan okur. */
const Live = {
  nodes: {}, now: 0, recv: 0, first: true, graphs: new Set(),
  KEYS: ['t', 'cpu', 'mem', 'ct', 'pw', 'rx', 'tx', 'in', 'tps'], GKEYS: ['gu', 'gm', 'gt', 'gp'],
  empty() { const o = {}; this.KEYS.concat(this.GKEYS).forEach(k => { o[k] = []; }); return o; },
  apply(p) {
    this.now = p.now; this.recv = performance.now();
    for (const [id, c] of Object.entries(p.nodes || {})) {
      const cur = this.nodes[id] || (this.nodes[id] = this.empty());
      const last = cur.t.length ? cur.t[cur.t.length - 1] : -1;
      const idx = c.t.findIndex(x => x > last);
      if (idx < 0) continue;
      const n0 = cur.t.length;
      this.KEYS.forEach(k => { for (let i = idx; i < c.t.length; i++) cur[k].push(c[k][i]); });
      this.GKEYS.forEach(k => {
        const src = c[k] || [];
        while (cur[k].length < src.length) cur[k].push(new Array(n0).fill(null));
        cur[k].forEach((arr, gi) => { const s = src[gi]; for (let i = idx; i < c.t.length; i++) arr.push(s ? s[i] : null); });
      });
      const cut = this.now - 3600; let k0 = 0;
      while (k0 < cur.t.length && cur.t[k0] < cut) k0++;
      if (k0 > 0) { this.KEYS.forEach(k => cur[k].splice(0, k0)); this.GKEYS.forEach(k => cur[k].forEach(a => a.splice(0, k0))); }
    }
  },
  ts() { return this.now + (performance.now() - this.recv) / 1000; },
  render() { const t = this.ts(); this.graphs.forEach(g => { if (!g.el.isConnected) this.graphs.delete(g); else g.draw(t); }); },
};
setInterval(() => { if (!document.hidden) Live.render(); }, 500);

/** Görev Yöneticisi tarzı çizgi grafik: renkli çerçeve, kayan ızgara, hafif dolgu. */
class Graph {
  constructor(el, o = {}) {
    this.o = Object.assign({ color: GC.cpu, min: 0, max: 100, win: 300, label: '', f: v => Math.round(v) + '%', grid: 10, auto: 1, cls: '' }, o);
    this.el = el;
    el.classList.add('gr');
    if (this.o.cls) this.o.cls.split(' ').forEach(c => el.classList.add(c));
    el.style.setProperty('--gc', this.o.color);
    el.innerHTML = '<svg viewBox="0 0 100 100" preserveAspectRatio="none"></svg><span class="gl"></span><span class="gv"></span><div class="mk"></div><div class="tip"></div>';
    [this.svg, this.gl, this.gv, this.mk, this.tip] = Array.from(el.children);
    this.gl.textContent = this.o.label;
    this.d = null; this.t = Live.ts();
    el.addEventListener('mousemove', e => this.hover(e));
    el.addEventListener('mouseleave', () => { this.tip.style.display = 'none'; this.mk.style.display = 'none'; });
    Live.graphs.add(this);
    this.draw(this.t);
  }
  setWin(w, grid) { this.o.win = w; if (grid) this.o.grid = grid; this.draw(Live.ts()); }
  draw(now) {
    const o = this.o, d = o.src ? o.src() : null;
    this.d = d; this.t = now;
    const win = o.win, t0 = now - win;
    let vmax = o.max;
    if (vmax == null) {
      let m = 0;
      if (d) d.lines.forEach(l => { for (let i = 0; i < d.t.length; i++) { const v = l.v[i]; if (v != null && d.t[i] >= t0 && v > m) m = v; } });
      vmax = niceMax(Math.max(m, o.auto));
    }
    const X = t => (t - t0) / win * 100, Y = v => 100 - clamp((v - o.min) / ((vmax - o.min) || 1)) * 100;
    let s = '<g class="grid">';
    for (const y of [25, 50, 75]) s += `<line x1="0" x2="100" y1="${y}" y2="${y}"/>`;
    for (let t = Math.ceil(t0 / o.grid) * o.grid; t <= now; t += o.grid) { const x = X(t).toFixed(2); s += `<line x1="${x}" x2="${x}" y1="0" y2="100"/>`; }
    s += '</g>';
    const vals = [];
    if (d) d.lines.forEach((l, li) => {
      let seg = [], last = null, lastV = null;
      const flush = () => {
        if (seg.length > 1) {
          const p = seg.map((q, i) => (i ? 'L' : 'M') + q[0].toFixed(2) + ' ' + q[1].toFixed(2)).join('');
          if (li === 0 && l.fill !== false) s += `<path class="fl"${l.color ? ` style="fill:${l.color}"` : ''} d="${p}L${seg[seg.length - 1][0].toFixed(2)} 100L${seg[0][0].toFixed(2)} 100Z"/>`;
          s += `<path class="ln"${l.color ? ` style="stroke:${l.color}"` : ''} d="${p}"/>`;
        }
        seg = [];
      };
      for (let i = 0; i < d.t.length; i++) {
        const t = d.t[i], v = l.v[i];
        if (t < t0 - 15) continue;
        if (v == null) { flush(); last = null; continue; }
        if (last != null && t - last > (o.gap || 12)) flush();
        seg.push([X(t), Y(v)]); last = t;
        if (now - t < (o.fresh || 20)) lastV = v;
      }
      flush();
      vals.push(lastV);
    });
    this.svg.innerHTML = s;
    this.gv.textContent = vals.length ? vals.map(v => v == null ? '—' : o.f(v)).join(' · ') : '—';
  }
  hover(e) {
    const d = this.d; if (!d || !d.t.length) return;
    const r = this.el.getBoundingClientRect(), fx = clamp((e.clientX - r.left) / r.width), t = this.t - this.o.win + fx * this.o.win;
    let bi = -1, bd = 1e12;
    for (let i = 0; i < d.t.length; i++) { const dd = Math.abs(d.t[i] - t); if (dd < bd) { bd = dd; bi = i; } }
    if (bi < 0 || bd > (this.o.gap || 12)) { this.tip.style.display = 'none'; this.mk.style.display = 'none'; return; }
    const ts = d.t[bi], x = (ts - (this.t - this.o.win)) / this.o.win * r.width;
    this.mk.style.display = 'block'; this.mk.style.left = x + 'px';
    this.tip.innerHTML = `<div style="color:#555">${this.o.dayfmt ? fmt.dt(ts) : fmt.time(ts)}</div>` + d.lines.map(l => `<div>${l.name ? esc(l.name) + ': ' : ''}<b>${l.v[bi] == null ? '—' : this.o.f(l.v[bi])}</b></div>`).join('');
    this.tip.style.display = 'block';
    const tw = this.tip.offsetWidth;
    this.tip.style.left = Math.max(2, Math.min(r.width - tw - 2, x + 10)) + 'px';
    this.tip.style.top = (r.height > 50 ? 18 : -4) + 'px';
  }
}

// metriğe göre grafik tanımları (genel bakış kutuları ve ayrıntı sayfası ortak kullanır)
const GSPEC = {
  cpu: { label: 'CPU', color: GC.cpu, src: d => ({ t: d.t, lines: [{ v: d.cpu }] }) },
  mem: { label: 'Bellek', color: GC.mem, src: d => ({ t: d.t, lines: [{ v: d.mem }] }) },
  gpu: { label: 'GPU', color: GC.gpu, src: d => ({ t: d.t, lines: d.gu.map((v, i) => ({ v, name: 'GPU ' + i, color: i ? '#2f7d00' : undefined, fill: i === 0 })) }) },
  vram: { label: 'VRAM', color: GC.vram, src: d => ({ t: d.t, lines: d.gm.map((v, i) => ({ v, name: 'GPU ' + i, color: i ? '#1c5a70' : undefined, fill: i === 0 })) }) },
  temp: { label: 'Sıcaklık', color: GC.temp, f: v => Math.round(v) + '°', src: d => ({ t: d.t, lines: d.gt.map((v, i) => ({ v, name: 'GPU ' + i })).concat([{ v: d.ct, name: 'CPU', color: '#c42b1c', fill: false }]) }) },
  pw: { label: 'Güç', color: GC.pw, max: null, auto: 50, f: v => Math.round(v) + ' W', src: d => ({ t: d.t, lines: [{ v: d.pw }] }) },
  net: { label: 'Ağ', color: GC.net, max: null, auto: 100000, f: fmt.bps, src: d => ({ t: d.t, lines: [{ v: d.rx, name: 'alınan' }, { v: d.tx, name: 'gönderilen', color: '#a15814', fill: false }]}) },
  tps: { label: 'Üretim', color: GC.tps, max: null, auto: 20, f: v => Math.round(v) + ' tok/sn', src: d => ({ t: d.t, lines: [{ v: d.tps }] }) },
  req: { label: 'Eşzamanlı istek', color: '#6b69d6', max: null, auto: 4, f: v => String(Math.round(v)), src: d => ({ t: d.t, lines: [{ v: d.in }] }) },
};
function gpuSpec(kind, i, name) {
  const key = kind === 'gpu' ? 'gu' : 'gm';
  return { label: (kind === 'gpu' ? 'GPU ' : 'VRAM ') + i + (name ? ' · ' + name : ''), color: kind === 'gpu' ? GC.gpu : GC.vram, src: d => ({ t: d.t, lines: [{ v: d[key][i] || [] }] }) };
}
function nodeGraph(el, spec, id, extra = {}) {
  const sp = typeof spec === 'string' ? GSPEC[spec] : spec;
  return new Graph(el, { ...sp, ...extra, src: () => { const d = Live.nodes[id]; return d ? sp.src(d) : null; } });
}

// ------------------------------------------------------------------ uygulama / yönlendirici
const App = {
  pages: {}, cur: null, timers: [], ov: null, subs: [], authState: {}, pollTimer: null,
  nav: [['', 'Genel bakış', 'grid'], ['nodes', 'Sunucular', 'server'], ['net', 'Ağ', 'network'], ['events', 'Olaylar', 'activity'], '-',
    ['site', 'Site bağlantısı', 'globe'], ['models', 'Modeller', 'layers'], ['dist', 'Dağıtım', 'box'], '-',
    ['chats', 'Sohbet kayıtları', 'users'], ['distill', 'Distillation', 'flask'], ['hf', 'HuggingFace', 'cloud'], '-',
    ['auto', 'Otomasyon', 'sliders'], ['settings', 'Ayarlar', 'gear']],
  register(id, page) { this.pages[id] = page; },
  every(fn, ms) {
    let stop = false, t;
    const loop = async () => { if (stop) return; if (!document.hidden) { try { await fn(); } catch (e) { if (!/Oturum/.test(e.message)) console.warn(e); } } t = setTimeout(loop, ms); };
    loop();
    this.timers.push(() => { stop = true; clearTimeout(t); });
  },
  onOverview(fn) { this.subs.push(fn); if (this.ov) fn(this.ov); },

  async init() {
    try { this.authState = await api('/api/auth/state', { noAuth: true }); }
    catch (e) { $('#root').innerHTML = `<div class="login"><div class="box"><p>Bağlanılamadı: ${esc(e.message)}</p></div></div>`; return; }
    if (!this.authState.configured) return this.showAuth('setup');
    if (!this.authState.authenticated) return this.showAuth('login');
    this.start();
  },

  showAuth(mode) {
    closeAllModals(); this.stopAll();
    const setup = mode === 'setup';
    $('#root').innerHTML = `<div class="login"><form class="box" autocomplete="on">
      <img class="lg" src="/static/logo.svg" alt="">
      ${setup ? '<p class="mu" style="margin:0 0 14px;text-align:center">Yönetici parolası belirleyin (en az 8 karakter)</p>' : ''}
      <label class="fld"><span>Parola</span><input class="inp" type="password" name="pw" autocomplete="${setup ? 'new-password' : 'current-password'}" autofocus></label>
      ${setup ? '<label class="fld"><span>Parola (tekrar)</span><input class="inp" type="password" name="pw2" autocomplete="new-password"></label>' : ''}
      <div class="err" id="auth-err" style="min-height:18px;font-size:12px"></div>
      <button class="btn pri" type="submit">${setup ? 'Kaydet' : 'Giriş'}</button>
      ${this.authState.demo ? '<p class="mu" style="margin:14px 0 0;font-size:11.5px;text-align:center">Demo parolası: demo1234</p>' : ''}
    </form></div>`;
    const form = $('form', $('#root'));
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const v = formVals(form), er = $('#auth-err');
      if (setup && v.pw !== v.pw2) { er.textContent = 'Parolalar eşleşmiyor'; return; }
      try {
        await api(setup ? '/api/auth/setup' : '/api/auth/login', { method: 'POST', body: { password: v.pw }, noAuth: true });
        this.authState = await api('/api/auth/state', { noAuth: true });
        this.start();
      } catch (err) { er.textContent = err.message; }
    });
  },

  stopAll() { this.timers.forEach(f => f()); this.timers = []; clearTimeout(this.pollTimer); this.subs = []; },

  start() {
    this.stopAll();
    Live.first = true; Live.nodes = {};
    const navHtml = this.nav.map(it => it === '-' ? '<hr>' : `<a href="#/${it[0]}" data-id="${it[0]}">${icon(it[2])}<span>${it[1]}</span><b class="cnt hide" data-cnt="${it[0]}"></b></a>`).join('');
    $('#root').innerHTML = `<div class="app">
      <aside class="side"><div class="brand"><img src="/static/logo.svg" alt=""></div><nav class="nav">${navHtml}</nav></aside>
      <header class="top"><h1 id="ptitle"></h1><span class="grow"></span>
        <span class="demo-tag hide" id="demo-tag">DEMO</span><span class="hstat" id="hstat"></span>
        <button class="btn ghost ic" id="out-btn" title="Çıkış">${icon('logout')}</button></header>
      <main class="main" id="view"></main></div>`;
    $('#out-btn').onclick = async () => { await post('/api/auth/logout').catch(() => { }); this.authState.authenticated = false; this.showAuth('login'); };
    if (this.authState.demo) $('#demo-tag').classList.remove('hide');
    window.onhashchange = () => this.route();
    this.route();
    this.pollOverview();
  },

  async pollOverview() {
    if (!document.hidden) {
      try {
        const url = Live.first ? '/api/live?window=900' : `/api/live?since=${(Live.now - 8).toFixed(1)}`;
        const [o, lv] = await Promise.all([api('/api/overview'), api(url)]);
        Live.apply(lv); Live.first = false; Live.render();
        this.ov = o; this.updateChrome(o);
        this.subs.forEach(fn => { try { fn(o); } catch (e) { console.error(e); } });
      } catch (e) { if (!/Oturum/.test(e.message)) console.warn(e); }
    }
    this.pollTimer = setTimeout(() => this.pollOverview(), 3000);
  },

  updateChrome(o) {
    const k = o.kpi, hs = $('#hstat'); if (!hs) return;
    hs.textContent = `${k.nodes_online}/${k.nodes_total} sunucu açık · ${k.replicas_ready} replika · ${fmt.n(k.rpm)} istek/dk`;
    const sc = $('[data-cnt="nodes"]'); if (sc) { sc.classList.remove('hide'); sc.textContent = `${k.nodes_online}/${k.nodes_total}`; sc.classList.toggle('alert', k.nodes_offline > 0); }
    const sn = $('[data-cnt="site"]'); if (sn) { const up = o.uplink?.state === 'up'; sn.classList.toggle('hide', up || o.uplink?.mode === 'off'); sn.classList.add('alert'); sn.textContent = '!'; }
    const dc = $('[data-cnt="dist"]'); if (dc) { dc.classList.toggle('hide', !o.transfers); dc.textContent = o.transfers; }
    const ac = $('[data-cnt="auto"]'); if (ac) { const u = o.autoscaler.unmet.length; ac.classList.toggle('hide', !u); ac.classList.add('alert'); ac.textContent = u; }
  },

  route() {
    closeAllModals();
    this.timers.forEach(f => f()); this.timers = []; this.subs = [];
    if (this.cur && this.cur.destroy) this.cur.destroy();
    const hash = location.hash.replace(/^#\/?/, '');
    const [id, ...args] = hash.split('/');
    const page = this.pages[id] || this.pages[''];
    this.cur = page;
    $$('.nav a').forEach(a => a.classList.toggle('on', a.dataset.id === (this.pages[id] ? id : '')));
    $('#ptitle').textContent = page.title;
    const old = $('#view'), view = old.cloneNode(false); old.replaceWith(view);  // önceki sayfanın dinleyicileri kalmasın
    page.mount(view, args);
    if (this.ov) this.subs.forEach(fn => fn(this.ov));
  },
};
const emptyBox = (title, text = '') => `<div class="empty"><b>${title}</b>${text ? '<br>' + text : ''}</div>`;
