'use strict';
/* Genel bakış: durum şeridi + her sunucu için canlı grafikler */

const shortGpu = n => String(n || '').replace(/NVIDIA GeForce |NVIDIA |AMD Radeon /g, '').replace(' Laptop GPU', ' Laptop');
function pcHw(n) {
  const hw = n.hw || {};
  const cpu = (hw.cpu?.model || '').replace(/\(R\)|\(TM\)|CPU|Processor|\d+-Core|with .*/g, '').replace(/\s+/g, ' ').trim();
  const gp = {};
  (hw.gpus || []).forEach(x => { const k = shortGpu(x.name) + ' ' + fmt.n(x.vram_gb, 0) + ' GB'; gp[k] = (gp[k] || 0) + 1; });
  const g = Object.entries(gp).map(([k, c]) => (c > 1 ? c + '× ' : '') + k).join(', ');
  return [cpu, hw.ram_gb ? fmt.n(hw.ram_gb, 0) + ' GB RAM' : '', g].filter(Boolean).join(' · ');
}
const tileKinds = n => (n.hw?.gpus || []).length ? ['cpu', 'mem', 'gpu', 'vram', 'temp', 'tps'] : ['cpu', 'mem', 'temp', 'tps', 'net', 'pw'];

App.register('', {
  title: 'Genel bakış',
  win: 300, tiles: new Map(), req: null,
  mount(el) {
    this.tiles = new Map(); this.req = null;
    el.innerHTML = `<div class="cmd"><span class="mu" style="padding:0 6px">Grafik aralığı</span>
        <div class="seg" id="ov-win">${[[60, '1 dk'], [300, '5 dk'], [900, '15 dk']].map(([w, l]) => `<button data-w="${w}" class="${w === this.win ? 'on' : ''}">${l}</button>`).join('')}</div>
        <span class="grow"></span><button class="cb" id="ov-add">${icon('search')}Ağı tara ve makine kur</button>
        <button class="cb" id="ov-tick">${icon('refresh')}Otomasyonu şimdi çalıştır</button></div>
      <div class="strip" id="ov-strip"></div><div id="ov-alert"></div>
      <div class="sec">Sunucular</div><div class="pcs" id="ov-pcs"></div>
      <div class="sec">Küme trafiği · son 30 dk</div>
      <div class="g3"><div id="ov-g1"></div><div id="ov-g2"></div><div id="ov-g3"></div></div>
      <div class="sec">Son olaylar</div><div class="card"><div class="feed" id="ov-events"></div></div>`;
    const mk = (id, label, color, f, key, extra = {}) => new Graph($('#' + id), { label, color, f, max: null, auto: 5, win: 1800, grid: 300, gap: 130, fresh: 130, cls: 'mid', ...extra,
      src: () => this.req ? { t: this.req.t, lines: [{ v: this.req[key] }] } : null });
    mk('ov-g1', 'İstek / dk', GC.req, v => Math.round(v), 'rpm', { auto: 5 });
    mk('ov-g2', 'Üretim (tok/sn)', GC.gpu, v => Math.round(v), 'tps', { auto: 20 });
    mk('ov-g3', 'İlk token süresi', GC.temp, v => fmt.ms(v), 'ttft', { auto: 200 });
    on(el, 'click', '#ov-win button', b => { this.win = +b.dataset.w; $$('#ov-win button').forEach(x => x.classList.toggle('on', x === b)); const grid = this.win <= 60 ? 10 : this.win <= 300 ? 30 : 60; this.tiles.forEach(t => t.graphs.forEach(g => g.setWin(this.win, grid))); });
    on(el, 'click', '[data-menu]', (b, e) => { e.stopPropagation(); const n = App.ov.nodes.find(x => x.id === +b.dataset.menu); if (n) popMenu(b, nodeMenuItems(n)); });
    on(el, 'click', '.pc-h', (h, e) => { if (!e.target.closest('button')) location.hash = '#/nodes/' + h.parentElement.dataset.id; });
    $('#ov-add').onclick = () => provisionWizard();
    $('#ov-tick').onclick = async () => { try { await post('/api/autoscaler/tick'); toast('Otomasyon değerlendirildi', 'ok'); } catch (e) { toastErr(e); } };
    App.onOverview(o => this.render(o));
  },
  render(o) {
    if (!$('#ov-strip')) return;
    const k = o.kpi, a = o.autoscaler, d = o.distill;
    const u = o.uplink || {}, siteTxt = { up: 'bağlı', connecting: 'bağlanıyor', down: 'kopuk', error: 'hata', off: 'kapalı' }[u.state] || u.state;
    const cells = [
      ['Site', `<span class="${u.state === 'up' ? 'ok' : u.state === 'off' ? '' : 'err'}">${esc(siteTxt)}</span>${u.state === 'up' && u.rtt_ms != null ? `<small>${u.rtt_ms} ms</small>` : ''}`],
      ['İnternet', o.net?.internet?.up ? `<span class="ok">var</span><small>${fmt.n(o.net.internet.rtt_ms)} ms</small>` : '<span class="err">yok</span>'],
      ['Sunucu', `${k.nodes_online}<small>/ ${k.nodes_total} açık</small>`], ['Uykuda', fmt.n(k.nodes_sleeping)],
      ['Replika', `${k.replicas_ready}<small>/ ${k.replicas_total} hazır</small>`], ['İstek', `${fmt.n(k.rpm)}<small>/dk</small>`],
      ['Üretim', `${fmt.n(k.tps)}<small>tok/sn</small>`], ['İlk token', k.ttft_ms ? `${fmt.n(k.ttft_ms)}<small>ms</small>` : '—'],
      ['Kuyruk', k.queue ? `<span class="warn">${k.queue}</span>` : '0'], ['Güç', `${fmt.n(k.power_w)}<small>W</small>`],
      ['Otomasyon', a.enabled ? (a.dry_run ? 'yalnız öneri' : 'açık') : 'kapalı'],
      ['Distillation', d.enabled ? ({ running: 'çalışıyor', waiting: 'bekliyor', idle: 'boşta' }[d.state] || d.state) : 'kapalı'],
    ];
    $('#ov-strip').innerHTML = cells.map(([t, v]) => `<div class="si"><span class="k">${t}</span><div class="v">${v}</div></div>`).join('');
    $('#ov-alert').innerHTML = a.unmet.length ? `<div class="alert">${icon('alert')}<div>Yer açılamadı: ${a.unmet.map(u => `<b>${esc(u.model)}</b> (${esc(u.reason)})`).join(', ')}</div></div>` : '';
    // sunucu kutuları
    const box = $('#ov-pcs'), seen = new Set();
    o.nodes.forEach((n, i) => {
      seen.add(n.id);
      let t = this.tiles.get(n.id);
      if (!t) {
        const el = document.createElement('div'); el.className = 'pc'; el.dataset.id = n.id;
        el.innerHTML = '<div class="pc-h"></div><div class="pc-hw"></div><div class="pc-g"></div><div class="pc-f"></div>';
        t = { el, sig: '', graphs: [] }; this.tiles.set(n.id, t);
      }
      if (box.children[i] !== t.el) box.insertBefore(t.el, box.children[i] || null);
      this.updTile(t, n);
    });
    for (const [id, t] of this.tiles) if (!seen.has(id)) { t.el.remove(); this.tiles.delete(id); }
    // küme trafiği (dakikalık)
    const rq = o.series.req;
    this.req = { t: rq.map(x => x.ts), rpm: rq.map(x => x.rpm), tps: rq.map(x => x.tps), ttft: rq.map(x => x.ttft || null) };
    $('#ov-events').innerHTML = o.events.slice(0, 10).map(e => `<div class="it"><time>${fmt.time(e.ts)}</time>${sq(e.level === 'error' ? 'err' : e.level === 'warn' ? 'warn' : '')}<div class="m"><span class="src">${esc(e.source)}</span>${esc(e.message)}</div></div>`).join('') || emptyBox('Henüz olay yok');
  },
  updTile(t, n) {
    const [st, cls] = nodeState(n), el = t.el, on_ = n.status === 'online';
    $('.pc-h', el).innerHTML = `${sq(cls)}<b>${esc(n.name)}</b><span class="tag">${esc(n.role)}</span>${n.is_self ? '<span class="tag" title="Kontrol sunucusu bu makinede çalışıyor">kontrol sunucusu</span>' : ''}<span class="mu">${esc(n.host)}</span>
      <span class="sp">${esc(st)}${on_ && n.uptime_s ? ' · ' + fmt.dur(n.uptime_s) : ''}<button class="btn ghost sm ic" data-menu="${n.id}" title="İşlemler">${icon('more')}</button></span>`;
    $('.pc-hw', el).textContent = pcHw(n) || '—';
    const sig = on_ ? 'on:' + (n.hw?.gpus || []).length : 'off:' + n.status;
    const g = $('.pc-g', el);
    if (t.sig !== sig) {
      t.sig = sig; g.innerHTML = ''; t.graphs = [];
      if (on_) tileKinds(n).forEach(kind => { const d = document.createElement('div'); g.appendChild(d); t.graphs.push(nodeGraph(d, kind, n.id, { win: this.win, grid: this.win <= 60 ? 10 : this.win <= 300 ? 30 : 60 })); });
    }
    if (!on_) {
      const msg = n.status === 'sleeping' ? 'Uyku modunda' + (n.last_seen ? ` · son ölçüm ${fmt.ago(n.last_seen)}` : '') + '<br>Talep gelince otomatik uyandırılır'
        : n.status === 'offline' ? `Bağlantı yok${n.last_error ? '<br><span class="mu">' + esc(n.last_error.slice(0, 80)) + '</span>' : ''}` : esc(NODE_ST[n.status]?.[0] || n.status) + '…';
      g.innerHTML = `<div class="gr-note">${msg}</div>`;
    }
    const deps = (n.deployments || []).filter(d => d.desired === 'running');
    $('.pc-f', el).innerHTML = (deps.map(d => `<span class="mchip" title="${esc(depLabel(d))}">${sq(d.state === 'ready' ? 'ok' : d.state === 'failed' ? 'err' : 'warn')}${esc(d.model)}${d.inflight ? ` <b>${d.inflight}</b>` : ''}</span>`).join('')
      || `<span class="mu">${on_ ? 'yüklü model yok' : ''}</span>`) + (n.power_w ? `<span class="mu" style="margin-left:auto">${fmt.w(n.power_w)}</span>` : '');
  },
});
