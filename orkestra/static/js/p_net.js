'use strict';
/* Ağ: modem (Superbox), switch ve makineler; ağ taraması ve SSH ile kurulum */

const LINK_WARN = n => {
  const l = n.net || {}, out = [];
  if (l.wifi) out.push('Wi-Fi ile bağlı: kablo takın');
  if (l.speed_mbps && l.speed_mbps < 1000) out.push(`Bağlantı ${l.speed_mbps} Mbit/sn: kablo ya da switch portunu kontrol edin`);
  if (l.errors > 0) out.push(`${fmt.n(l.errors)} ağ hatası`);
  return out;
};
const speedText = m => m ? (m >= 1000 ? (m / 1000).toLocaleString('tr-TR') + ' Gbit/sn' : m + ' Mbit/sn') : '—';

function provisionWizard(prefill = {}) {
  let hosts = [], jobs = new Map(), timer = null;
  const dlg = modal({ title: 'Ağı tara ve makine kur', size: 'xl', onClose: () => clearInterval(timer), body: `
    <div class="row wrap mb"><input class="inp mono" id="pv-cidr" style="width:180px" placeholder="otomatik (bu ağ)" value="${esc(prefill.cidr || '')}">
      <button class="btn" id="pv-scan">${icon('search')}Ağı tara</button><span class="mu" id="pv-sum"></span><span class="grow"></span>
      <button class="btn" id="pv-self" title="Bu bilgisayarın GPU'sunu da kümeye ekler">${icon('plus')}Bu bilgisayarı ekle</button><button class="btn" id="pv-manual">Elle ekle…</button></div>
    <div class="card mb" style="max-height:230px;overflow:auto"><table class="tbl"><thead><tr><th style="width:30px"></th><th>Adres</th><th>Durum</th><th>Sistem</th><th>MAC</th></tr></thead><tbody id="pv-hosts"><tr><td colspan="5">${emptyBox('Taranıyor…')}</td></tr></tbody></table></div>
    <form id="pv-f"><div class="form-g c3">
      ${fld('Adres (listede yoksa)', '<input class="inp mono" name="extra" placeholder="192.168.1.50">')}${fld('SSH kullanıcısı', `<input class="inp" name="user" value="${esc(localStorage.getItem('pv-user') || '')}" placeholder="ubuntu" autocomplete="off">`)}
      ${fld('SSH parolası', '<input class="inp" type="password" name="password" autocomplete="new-password">', 'Saklanmaz; sudo için de kullanılır')}</div>
      <div class="row wrap mt" style="gap:18px">${chk('ffmpeg', 'ffmpeg (ses biçimleri)', true)}${chk('piper', 'Piper (Türkçe metin → konuşma)', false)}${chk('build_tools', 'Derleme araçları (yamalı llama.cpp\'yi makinede derlemek için)', false)}${chk('gpu_driver', 'NVIDIA sürücüsü yoksa kur', false)}</div>
      <details class="mt"><summary class="mu" style="cursor:pointer">SSH anahtarı ile giriş</summary><div class="mt">${fld('Özel anahtar (içerik)', '<textarea class="inp mono" name="private_key" rows="3" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea>', 'Parola varsa anahtarın parolası sayılır')}</div></details></form>
    <div id="pv-jobs" class="mt"></div>`,
    actions: [{ label: 'Kapat' }, { label: 'Seçili makinelere kur', kind: 'pri', icon: 'download', close: false, onClick: () => start() }] });
  const scan = async () => {
    const tb = $('#pv-hosts', dlg.el); tb.innerHTML = `<tr><td colspan="5">${emptyBox('Taranıyor…')}</td></tr>`;
    try {
      const r = await post('/api/net/scan', { cidr: $('#pv-cidr', dlg.el).value.trim() });
      hosts = r.hosts; $('#pv-sum', dlg.el).textContent = `${r.cidr} · ${r.scanned} adres · ${hosts.length} cihaz`;
      const label = { modem: ['Modem', 'info'], self: ['Bu bilgisayar', 'info'], node: ['Kümede', 'ok'], agent: ['Ajan var, kayıtsız', 'warn'], ubuntu: ['Kuruluma hazır', 'idle'], ssh: ['SSH açık', 'idle'], other: ['Diğer cihaz', 'idle'] };
      tb.innerHTML = hosts.map(h => { const [t, c] = label[h.kind] || ['?', 'idle'], can = ['ubuntu', 'ssh', 'agent', 'node'].includes(h.kind) && h.ssh;
        return `<tr><td><input type="checkbox" data-ip="${esc(h.ip)}" ${h.kind === 'ubuntu' ? 'checked' : ''} ${can ? '' : 'disabled'}></td><td class="mono">${esc(h.ip)}${h.node ? ` <span class="mu">${esc(h.node)}</span>` : ''}</td><td>${badge(t, c)}</td><td class="mu">${esc((h.banner || '').replace(/^SSH-2\.0-/, '') || '—')}</td><td class="mono mu">${esc(h.mac || '—')}</td></tr>`; }).join('') || `<tr><td colspan="5">${emptyBox('Cihaz bulunamadı')}</td></tr>`;
    } catch (e) { tb.innerHTML = `<tr><td colspan="5"><div class="empty err">${esc(e.message)}</div></td></tr>`; }
  };
  const drawJobs = () => {
    const box = $('#pv-jobs', dlg.el); if (!box) return;
    box.innerHTML = [...jobs.values()].map(j => `<div class="card mb"><div class="card-h">${sq(j.state === 'done' ? 'ok' : j.state === 'failed' ? 'err' : 'warn')}<h3>${esc(j.name || j.host)}</h3><span class="mu">${esc(j.state === 'running' ? j.step : j.state === 'done' ? 'tamamlandı' : j.error)}</span>${j.reboot ? '<span class="tag">yeniden başlatma gerekli</span>' : ''}</div><pre class="log" style="max-height:130px;border:0">${esc((j.log || []).slice(-40).join('\n'))}</pre></div>`).join('');
    $$('pre', box).forEach(p => { p.scrollTop = p.scrollHeight; });
  };
  const poll = async () => {
    for (const [id, j] of jobs) if (j.state === 'running') { try { jobs.set(id, await api('/api/provision/' + id)); } catch (e) { /* sonraki turda */ } }
    drawJobs();
    if (![...jobs.values()].some(j => j.state === 'running')) { clearInterval(timer); timer = null; scan(); }
  };
  const start = async () => {
    const v = formVals($('#pv-f', dlg.el)), ips = $$('[data-ip]:checked', dlg.el).map(x => x.dataset.ip);
    if (v.extra.trim()) ips.push(v.extra.trim());
    if (!ips.length) return toast('Kurulacak makine seçin', 'warn');
    if (!v.user.trim() || (!v.password && !v.private_key.trim())) return toast('SSH kullanıcısı ve parolası gerekli', 'warn');
    try { localStorage.setItem('pv-user', v.user.trim()); } catch (e) { /* yoksay */ }
    for (const ip of ips) {
      const j = await post('/api/provision', { host: ip, user: v.user.trim(), password: v.password, private_key: v.private_key.trim(), ffmpeg: v.ffmpeg, piper: v.piper, build_tools: v.build_tools, gpu_driver: v.gpu_driver });
      jobs.set(j.id, j);
    }
    $('[name=password]', dlg.el).value = ''; drawJobs();
    if (!timer) timer = setInterval(poll, 1200);
    return false;
  };
  $('#pv-scan', dlg.el).onclick = scan;
  $('#pv-manual', dlg.el).onclick = () => { dlg.close(); addNodeWizard(); };
  $('#pv-self', dlg.el).onclick = async () => { try { await post('/api/nodes/self'); toast('Bu bilgisayar kümeye eklendi', 'ok'); dlg.close(); location.hash = '#/nodes'; } catch (e) { toastErr(e); } };
  scan();
}

App.register('net', {
  title: 'Ağ', data: null, tiles: new Map(), gs: [],
  mount(el) {
    this.data = null; this.tiles = new Map();
    el.innerHTML = `<div class="cmd"><button class="cb" id="n-scan">${icon('search')}Ağı tara ve makine kur</button><button class="cb" id="n-bw">${icon('activity')}Hızları ölç</button><span class="grow"></span><span class="mu" id="n-local"></span></div>
      <div class="topo" id="n-topo"></div><div id="n-alert"></div>
      <div class="sec">İnternet çıkışı</div><div class="g3"><div id="n-g1"></div><div id="n-g2"></div><div id="n-g3"></div></div>
      <div class="sec">Switch üzerindeki makineler</div><div class="lan" id="n-lan"></div>`;
    const mk = (id, label, color, f, lines, extra = {}) => new Graph($('#' + id), { label, color, f, max: null, auto: 10, win: 900, grid: 60, gap: 30, fresh: 30, cls: 'mid', ...extra, src: () => { const h = this.data?.hist; return h ? { t: h.t, lines: lines(h) } : null; } });
    mk('n-g1', 'Modem gecikmesi', GC.net, v => fmt.n(v, 1) + ' ms', h => [{ v: h.modem }], { auto: 5 });
    mk('n-g2', 'İnternet gecikmesi', GC.temp, v => Math.round(v) + ' ms', h => [{ v: h.inet }], { auto: 50 });
    mk('n-g3', 'İnternet trafiği', GC.cpu, fmt.bps, h => [{ v: h.down, name: 'inen' }, { v: h.up, name: 'giden', color: '#a15814', fill: false }], { auto: 100000 });
    $('#n-scan').onclick = () => provisionWizard();
    $('#n-bw').onclick = async () => {
      const ns = (App.ov?.nodes || []).filter(n => n.status === 'online'); toast('Ana PC ile makineler arası hız ölçülüyor…', 'info', 2500);
      for (const n of ns) { try { await post(`/api/fabric/nodes/${n.id}/bwtest`); await this.load(); } catch (e) { toast(`${n.name}: ${e.message}`, 'err'); } }
    };
    on(el, 'click', '[data-bw]', async b => { try { b.disabled = true; await post(`/api/fabric/nodes/${b.dataset.bw}/bwtest`); await this.load(); } catch (e) { toastErr(e); } finally { b.disabled = false; } });
    App.every(() => this.load(), 3000);
    App.onOverview(() => this.draw());
  },
  async load() { this.data = await api('/api/net'); this.draw(); },
  draw() {
    const d = this.data, top = $('#n-topo'), ov = App.ov; if (!d || !top || !ov) return;
    const L = d.local || {}, M = d.modem || {}, I = d.internet || {}, U = d.uplink || {}, nodes = ov.nodes;
    $('#n-local').textContent = L.ip ? `Bu bilgisayar ${L.ip} · ${L.iface || '?'} · ${speedText(L.speed_mbps)}` : '';
    const box = (cls, title, sub, st) => `<div class="tn ${cls}">${sq(st)}<div><b>${esc(title)}</b><small>${sub}</small></div></div>`;
    const up = U.state === 'up', lanOn = nodes.filter(n => n.status === 'online').length;
    top.innerHTML = box('', 'Site (hosting)', up ? esc(U.public_url || 'bağlı') : U.mode === 'off' ? 'bağlantı kapalı' : esc(U.error || U.state), up ? 'ok' : U.mode === 'off' ? '' : 'err') + '<i class="tl"></i>' +
      box('', 'İnternet', I.up ? `${fmt.n(I.rtt_ms)} ms` : 'erişilemiyor', I.up ? 'ok' : 'err') + '<i class="tl"></i>' +
      box('', M.name || 'Modem', `modem · ${esc(M.ip || '—')}${M.rtt_ms != null ? ' · ' + fmt.n(M.rtt_ms, 1) + ' ms' : ''}`, M.up ? 'ok' : 'err') + '<i class="tl"></i>' +
      box('', 'Switch', `${lanOn}/${nodes.length} makine açık`, lanOn ? 'ok' : 'warn');
    const warn = []; nodes.forEach(n => { if (n.status === 'online') LINK_WARN(n).forEach(w => warn.push(`<b>${esc(n.name)}</b>: ${esc(w)}`)); });
    $('#n-alert').innerHTML = warn.length ? `<div class="alert mt">${icon('alert')}<div>${warn.join('<br>')}</div></div>` : '';
    const box2 = $('#n-lan'), seen = new Set();
    nodes.forEach((n, i) => {
      seen.add(n.id); let t = this.tiles.get(n.id);
      if (!t) { const e = document.createElement('div'); e.className = 'lm'; e.innerHTML = '<div class="lm-h"></div><div class="lm-g"></div><div class="lm-f"></div>'; t = { el: e, g: nodeGraph($('.lm-g', e), 'net', n.id, { win: 300, grid: 30, cls: 'lm-gr' }) }; this.tiles.set(n.id, t); }
      if (box2.children[i] !== t.el) box2.insertBefore(t.el, box2.children[i] || null);
      const l = n.net || {}, on_ = n.status === 'online', b = (d.bw || {})[n.id], [st, c] = nodeState(n);
      $('.lm-h', t.el).innerHTML = `${sq(c)}<b>${esc(n.name)}</b><span class="mu mono">${esc(l.ip || n.host)}</span><span class="sp">${esc(st)}</span>`;
      $('.lm-f', t.el).innerHTML = `<span class="${l.speed_mbps && l.speed_mbps < 1000 ? 'warn' : ''}">${on_ ? speedText(l.speed_mbps) : '—'}${l.wifi ? ' · Wi-Fi' : ''}</span><span class="mu mono">${esc(n.mac || '')}</span><span class="grow"></span>${b ? `<span title="${fmt.ago(b.ts)}">ölçülen ${fmt.n(b.mbit)} Mbit/sn</span>` : ''}<button class="btn sm" data-bw="${n.id}" ${on_ ? '' : 'disabled'}>Ölç</button>`;
      t.g.el.classList.toggle('off', !on_);
    });
    for (const [id, t] of this.tiles) if (!seen.has(id)) { t.el.remove(); this.tiles.delete(id); }
  },
});
