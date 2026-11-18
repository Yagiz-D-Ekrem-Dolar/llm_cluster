'use strict';
/* Sunucular: Görev Yöneticisi "Performans" sekmesi gibi ana-ayrıntı görünümü */

async function nodePower(n, action) {
  const names = { wake: 'Uyandır', sleep: 'Uykuya al', reboot: 'Yeniden başlat', poweroff: 'Kapat' };
  if (['sleep', 'reboot', 'poweroff'].includes(action)) {
    const hasLoad = (n.deployments || []).some(d => d.inflight > 0);
    if (!await confirmBox(`<b>${esc(n.name)}</b> için “${names[action]}” uygulanacak. Üzerindeki modeller önce durdurulur.` +
      (hasLoad ? '<br><span class="warn">İşlenen istekler var; devam ederseniz kesilirler.</span>' : ''), { title: names[action], ok: names[action], danger: action !== 'sleep' })) return;
  }
  try { await post(`/api/nodes/${n.id}/power`, { action, force: true }); toast(`${n.name}: ${names[action] || action} komutu gönderildi`, 'ok'); }
  catch (e) { toastErr(e); }
}
async function nodeToggleMaint(n) {
  try { await post(`/api/nodes/${n.id}/power`, { action: n.enabled ? 'disable' : 'enable' }); toast(n.enabled ? 'Bakım moduna alındı' : 'Bakımdan çıkarıldı', 'ok'); } catch (e) { toastErr(e); }
}
function nodeMenuItems(n) {
  const on_ = n.status === 'online';
  return [
    { label: 'Ayrıntılar', icon: 'eye', onClick: () => { location.hash = '#/nodes/' + n.id; } }, '-',
    { label: 'Uyandır (Wake-on-LAN)', icon: 'power', disabled: on_ || n.status === 'waking', onClick: () => nodePower(n, 'wake') },
    { label: 'Uykuya al', icon: 'moon', disabled: !on_, onClick: () => nodePower(n, 'sleep') },
    { label: 'Yeniden başlat', icon: 'refresh', disabled: !on_, onClick: () => nodePower(n, 'reboot') },
    { label: n.enabled ? 'Bakım moduna al' : 'Bakımdan çıkar', icon: 'gear', onClick: () => nodeToggleMaint(n) },
  ];
}
const lastOf = a => { for (let i = (a || []).length - 1; i >= 0; i--) if (a[i] != null) return a[i]; return null; };

App.register('nodes', {
  title: 'Sunucular',
  sel: null, mode: 'live', win: 300, hist: null, items: new Map(), gsig: '', dsel: null, histTimer: null,
  mount(el, args) {
    this.sel = args[0] ? +args[0] : null; this.mode = 'live'; this.win = 300; this.hist = null; this.items = new Map(); this.gsig = ''; this.dsel = null;
    el.innerHTML = `<div class="cmd" id="nd-cmd">
        <button class="cb" id="c-add">${icon('search')}Ağı tara ve kur</button><span class="sep"></span>
        <button class="cb" data-a="wake">${icon('power')}Uyandır</button><button class="cb" data-a="sleep">${icon('moon')}Uykuya al</button>
        <button class="cb" data-a="reboot">${icon('refresh')}Yeniden başlat</button><button class="cb" data-a="maint">${icon('gear')}Bakım modu</button><span class="sep"></span>
        <button class="cb" data-a="deploy">${icon('layers')}Model dağıt…</button><button class="cb" data-a="settings">${icon('edit')}Ayarlar…</button>
        <button class="cb" data-a="install">${icon('download')}Kurulum komutu</button></div>
      <div class="md"><div class="md-l" id="md-l"></div><div class="md-r" id="md-r"></div></div>`;
    $('#c-add').onclick = () => provisionWizard();
    on(el, 'click', '.md-i', t => { this.sel = +t.dataset.id; history.replaceState(null, '', '#/nodes/' + this.sel); this.render(App.ov); });
    on(el, 'click', '[data-a]', b => this.command(b.dataset.a));
    on(el, 'click', '[data-rng]', b => this.setRange(b.dataset.rng));
    on(el, 'click', '[data-dep]', b => this.depAction(b.dataset.dep, +b.dataset.id, b.dataset.n ? +b.dataset.n : null));
    App.onOverview(o => this.render(o));
  },
  destroy() { clearInterval(this.histTimer); },
  node() { return (App.ov?.nodes || []).find(n => n.id === this.sel); },
  command(a) {
    const n = this.node(); if (!n) return;
    if (a === 'maint') return nodeToggleMaint(n);
    if (a === 'settings') return nodeSettings(n.id);
    if (a === 'install') return nodeInstall(n.id);
    if (a === 'deploy') return deployToNode(n);
    nodePower(n, a);
  },
  async depAction(act, id, nodeId) {
    if (act === 'log') return openLogs(nodeId, `dep-${id}`);
    if (act === 'remove' && !await confirmBox('Bu dağıtım kaldırılacak.', { danger: true, ok: 'Kaldır' })) return;
    try { await post(`/api/deployments/${id}/action`, { action: act }); } catch (e) { toastErr(e); }
  },
  render(o) {
    const list = $('#md-l'); if (!list) return;
    const nodes = o.nodes;
    nodes.forEach(n => {
      let it = this.items.get(n.id);
      if (!it) {
        const el = document.createElement('div'); el.className = 'md-i'; el.dataset.id = n.id;
        el.innerHTML = '<div class="g"></div><div><b></b><small></small></div>';
        it = { el, g: nodeGraph($('.g', el), 'cpu', n.id, { cls: 'mini', win: 300, grid: 30 }) }; this.items.set(n.id, it);
      }
      const on_ = n.status === 'online', gp = n.gpus || [], gu = gp.length ? gp.reduce((a, x) => a + (x.util || 0), 0) / gp.length : null;
      $('b', it.el).textContent = n.name;
      $('small', it.el).textContent = on_ ? `CPU ${fmt.pct(n.cpu)} · Bellek ${fmt.pct(n.mem?.pct)}${gu != null ? ' · GPU ' + fmt.pct(gu) : ''}` : nodeState(n)[0];
      it.g.el.classList.toggle('off', !on_);
    });
    for (const [id, it] of this.items) if (!nodes.find(n => n.id === id)) { it.el.remove(); this.items.delete(id); }
    nodes.forEach((n, i) => { const it = this.items.get(n.id); if (list.children[i] !== it.el) list.insertBefore(it.el, list.children[i] || null); });
    if (this.sel == null || !nodes.find(n => n.id === this.sel)) this.sel = nodes[0]?.id ?? null;
    this.items.forEach((it, id) => it.el.classList.toggle('on', id === this.sel));
    const n = this.node();
    const cmd = $('#nd-cmd');
    if (cmd && n) {
      const on_ = n.status === 'online';
      $('[data-a=wake]', cmd).disabled = on_ || n.status === 'waking';
      ['sleep', 'reboot'].forEach(a => { $(`[data-a=${a}]`, cmd).disabled = !on_; });
      $('[data-a=maint]', cmd).lastChild.textContent = n.enabled ? 'Bakım modu' : 'Bakımdan çıkar';
    }
    if (cmd) $$('[data-a]:not([data-a=wake]):not([data-a=sleep]):not([data-a=reboot]):not([data-a=maint])', cmd).forEach(b => { b.disabled = !n; });
    this.detail(n);
  },
  setRange(r) {
    const live = { l1: 60, l5: 300, l15: 900 };
    if (live[r]) { this.mode = 'live'; this.win = live[r]; this.hist = null; clearInterval(this.histTimer); }
    else { this.mode = r; this.fetchHist(); clearInterval(this.histTimer); this.histTimer = setInterval(() => this.fetchHist(), 10000); }
    this.gsig = ''; this.detail(this.node());
  },
  async fetchHist() {
    if (this.mode === 'live' || this.sel == null) return;
    try { const h = await api(`/api/nodes/${this.sel}/history?range=${this.mode}`); h.range = this.mode; this.hist = h; } catch (e) { /* sessiz */ }
  },
  rangeBtns() {
    const cur = this.mode === 'live' ? { 60: 'l1', 300: 'l5', 900: 'l15' }[this.win] : this.mode;
    const b = ([k, l]) => `<button data-rng="${k}" class="${k === cur ? 'on' : ''}">${l}</button>`;
    return `<div class="row wrap mb"><span class="mu">Canlı</span><div class="seg">${[['l1', '1 dk'], ['l5', '5 dk'], ['l15', '15 dk']].map(b).join('')}</div>
      <span class="mu" style="margin-left:10px">Geçmiş</span><div class="seg">${[['1h', '1 saat'], ['6h', '6 saat'], ['24h', '24 saat'], ['7d', '7 gün']].map(b).join('')}</div></div>`;
  },
  detail(n) {
    const r = $('#md-r'); if (!r) return;
    if (!n) { r.innerHTML = emptyBox('Sunucu yok', '“Ağı tara ve kur” ile switch üzerindeki makineleri ekleyin.'); this.dsel = null; return; }
    if (this.dsel !== n.id) { this.dsel = n.id; this.gsig = ''; this.hist = null; r.innerHTML = '<div id="d-head"></div><div id="d-alert"></div><div id="d-rng"></div><div class="dgrid" id="d-g"></div><div class="facts" id="d-facts"></div><div class="sec">Donanım ve bağlantı</div><div id="d-info"></div><div class="sec">Bu sunucudaki modeller</div><div id="d-deps"></div>'; if (this.mode !== 'live') this.fetchHist(); }
    const [st, cls] = nodeState(n), hw = n.hw || {}, on_ = n.status === 'online', mem = n.mem || {}, gp = n.gpus || [];
    $('#d-head').innerHTML = `<div class="dh"><h2>${esc(n.name)}</h2>${badge(st, cls)}<span class="tag">${esc(n.role)}</span>${n.is_self ? '<span class="tag" title="Kontrol sunucusu bu makinede çalışıyor">kontrol sunucusu</span>' : ''}<span class="mu">${esc(n.host)}</span><span class="hwt">${esc(pcHw(n))}</span></div>`;
    const al = [];
    if (n.gpu_error) al.push(['err', 'GPU hatası: ' + n.gpu_error]);
    if (!on_ && n.last_error) al.push(['err', n.last_error]);
    (n.health_reasons || []).forEach(x => al.push(['warn', x]));
    $('#d-alert').innerHTML = al.map(([c, t]) => `<div class="alert ${c === 'err' ? 'err' : ''}">${icon('alert')}<div>${esc(t)}</div></div>`).join('');
    $('#d-rng').innerHTML = this.rangeBtns();
    // grafikler: yalnızca yapı değişince yeniden kur
    const ng = (hw.gpus || []).length, sig = `${n.id}|${this.mode}|${this.win}|${ng}`;
    if (sig !== this.gsig) {
      this.gsig = sig;
      const g = $('#d-g'); g.innerHTML = '';
      const add = (spec, name) => {
        const cap = document.createElement('div'); const d = document.createElement('div'); cap.appendChild(d); g.appendChild(cap);
        const live = this.mode === 'live', span = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800 }[this.mode];
        const sp = typeof spec === 'string' ? GSPEC[spec] : spec;
        if (live) { nodeGraph(d, sp, n.id, { cls: 'big', win: this.win, grid: this.win <= 60 ? 10 : this.win <= 300 ? 30 : 60 }); return; }
        const hmap = this.histSrc(spec, ng);
        new Graph(d, { ...sp, cls: 'big', win: span, grid: span / 6, gap: (this.hist?.bucket || 60) * 2.6, fresh: (this.hist?.bucket || 60) * 2.6, dayfmt: span > 86400, src: () => this.hist && this.hist.range === this.mode ? hmap(this.hist) : null });
      };
      ['cpu', 'mem'].forEach(k => add(k));
      if (this.mode === 'live') { for (let i = 0; i < ng; i++) { add(gpuSpec('gpu', i, shortGpu(hw.gpus[i].name))); add(gpuSpec('vram', i, shortGpu(hw.gpus[i].name))); } }
      else if (ng) { add('gpu'); add('vram'); }
      ['temp', 'pw', 'net'].forEach(k => add(k));
      add(this.mode === 'live' ? 'tps' : 'req'); if (this.mode === 'live') add('req');
    }
    // değerler
    const gu = gp.length ? gp.reduce((a, x) => a + (x.util || 0), 0) / gp.length : null;
    const vu = gp.reduce((a, x) => a + (x.mem_used_gb || 0), 0), vt = gp.reduce((a, x) => a + (x.mem_total_gb || 0), 0), ld = Live.nodes[n.id];
    const facts = [['CPU', fmt.pct(n.cpu)], ['Bellek', `${fmt.n(mem.used_gb, 1)}<small> / ${fmt.n(mem.total_gb, 0)} GB</small>`]];
    if (gp.length) facts.push(['GPU', fmt.pct(gu)], ['VRAM', `${fmt.n(vu, 1)}<small> / ${fmt.n(vt, 0)} GB</small>`]);
    facts.push(['Sıcaklık', fmt.temp(n.max_temp)], ['Güç', fmt.w(n.power_w)], ['Üretim', `${fmt.n(ld ? lastOf(ld.tps) : null)}<small> tok/sn</small>`],
      ['Eşzamanlı istek', String((n.deployments || []).reduce((a, d) => a + d.inflight, 0))], ['Çalışma süresi', fmt.dur(n.uptime_s)]);
    $('#d-facts').innerHTML = on_ ? facts.map(([k, v]) => `<div class="f"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('') : '';
    $('#d-info').innerHTML = `<div class="g2"><dl class="kv"><dt>Adres</dt><dd class="mono">${esc(n.host)}:${n.agent_port}</dd><dt>MAC</dt><dd class="mono">${esc(n.mac || '—')}</dd>
      <dt>İşletim sistemi</dt><dd>${esc(hw.os || '—')}</dd><dt>İşlemci</dt><dd>${esc(hw.cpu ? `${hw.cpu.model} (${hw.cpu.cores} çekirdek / ${hw.cpu.threads} iş parçacığı)` : '—')}</dd></dl>
      <dl class="kv"><dt>Ağ bağlantısı</dt><dd>${on_ && n.net ? `${n.net.speed_mbps ? speedText(n.net.speed_mbps) : 'hız bilinmiyor'}${n.net.wifi ? ' · <span class="warn">Wi-Fi</span>' : ''} · ${esc(n.net.iface || '')}` : '—'}</dd><dt>Güç</dt><dd>${n.power?.battery_pct != null ? `${n.power.ac === false ? '<span class="warn">Pilde</span>' : 'Şebeke'} · %${fmt.n(n.power.battery_pct)}` : on_ ? 'Şebeke' : '—'}</dd><dt>Hızlandırıcı</dt><dd>${chips(hw.gpu_caps?.backends) || '—'}${hw.gpu_caps?.driver ? ` <span class="mu">sürücü ${esc(hw.gpu_caps.driver)}</span>` : ''}</dd><dt>GPU'lar</dt><dd>${(hw.gpus || []).map(g => `${g.index}: ${esc(shortGpu(g.name))} · ${fmt.n(g.vram_gb, 0)} GB`).join('<br>') || 'yok'}</dd><dt>Ajan</dt><dd>${esc(hw.agent || '—')}</dd>
      <dt>Etiketler</dt><dd>${chips(n.tags) || '—'}</dd><dt>Son görülme</dt><dd>${on_ ? 'şimdi' : fmt.ago(n.last_seen)}${n.idle_s > 60 ? ` · boşta ${fmt.dur(n.idle_s)}` : ''}</dd></dl></div>`;
    const deps = n.deployments || [];
    $('#d-deps').innerHTML = deps.length ? `<div class="card">` + deps.map(d => `<div class="it row" style="padding:7px 12px;border-bottom:1px solid var(--line)">
        ${sq(d.state === 'ready' ? 'ok' : d.state === 'failed' ? 'err' : d.state === 'stopped' ? '' : 'warn')}<b>${esc(d.model)}</b><span class="mono mu">:${d.port}</span>
        <span class="mu">${esc(depLabel(d))}</span>${d.pinned ? '<span class="tag">sabit</span>' : '<span class="tag">otomatik</span>'}<span class="tag">${d.gpus.length ? 'GPU ' + d.gpus.join(',') : 'CPU'}</span>
        <span class="mu">iş ${d.inflight}/${d.max_conc}${d.tps ? ' · ' + d.tps + ' tok/sn' : ''}</span>${d.error ? `<span class="err ell" style="max-width:260px" title="${esc(d.error)}">${esc(d.error)}</span>` : ''}
        <span class="grow"></span><button class="btn sm" data-dep="log" data-id="${d.id}" data-n="${n.id}">Günlük</button>
        <button class="btn sm" data-dep="${d.desired === 'stopped' ? 'start' : 'stop'}" data-id="${d.id}">${d.desired === 'stopped' ? 'Başlat' : 'Durdur'}</button>
        <button class="btn sm" data-dep="restart" data-id="${d.id}">Yeniden başlat</button><button class="btn sm danger" data-dep="remove" data-id="${d.id}">Kaldır</button></div>`).join('') + '</div>'
      : `<div class="card">${emptyBox('Bu sunucuda model yok', 'Talep geldiğinde otomatik yüklenir ya da “Model dağıt…” ile elle yükleyebilirsiniz.')}</div>`;
  },
  histSrc(spec, ng) {
    const key = typeof spec === 'string' ? spec : (spec.label.startsWith('GPU') ? 'gpu' : 'vram');
    return h => ({ t: h.ts, lines: ({
      cpu: [{ v: h.cpu }], mem: [{ v: h.mem }], gpu: [{ v: h.gpu_util }], vram: [{ v: h.gpu_mem }],
      temp: [{ v: h.gpu_temp, name: 'GPU' }, { v: h.cpu_temp, name: 'CPU', color: '#c42b1c', fill: false }],
      pw: [{ v: h.power }], net: [{ v: h.net }], req: [{ v: h.inflight }], tps: [{ v: [] }],
    })[key] });
  },
});

function deployToNode(n) {
  api('/api/models').then(ms => {
    const llm = ms.filter(m => m.enabled);
    if (!llm.length) return toast('Önce Modeller sayfasından bir model tanımlayın', 'warn');
    const m = modal({ title: `${esc(n.name)} üzerine model dağıt`, body: `<form id="dm">${fld('Model', `<select class="inp" name="model">${llm.map(x => opt(x.id, `${x.name} (${x.size_gb} GB)`, '')).join('')}</select>`)}
      <div class="mt">${chk('force', 'Kapasite uygun görünmese de dene', false, 'VRAM/RAM tahmini yetersizse normalde reddedilir')}</div></form>`,
      actions: [{ label: 'Vazgeç' }, { label: 'Dağıt', kind: 'pri', onClick: async mm => {
        const v = formVals($('#dm', mm.el)); await post(`/api/models/${v.model}/deploy`, { node_id: n.id, force: v.force }); toast('Dağıtım başlatıldı', 'ok');
      } }] });
  }).catch(toastErr);
}

async function openLogs(nodeId, proc) {
  const m = modal({ title: `Günlük · ${esc(proc)}`, size: 'xl', body: '<pre class="log" id="lg">yükleniyor…</pre>', actions: [{ label: 'Yenile', icon: 'refresh', close: false, onClick: () => load() }, { label: 'Kapat' }] });
  const load = async () => { try { const r = await api(`/api/nodes/${nodeId}/logs?proc=${encodeURIComponent(proc)}&tail=300`); const p = $('#lg', m.el); p.textContent = r.text || '(boş)'; p.scrollTop = p.scrollHeight; } catch (e) { $('#lg', m.el).textContent = e.message; } };
  load();
}

async function nodeSettings(id) {
  const [r, rts] = await Promise.all([api(`/api/nodes/${id}`), api('/api/runtimes')]), n = r.node;
  const m = modal({ title: `${esc(n.name)} · ayarlar`, size: 'wide', body: `<form id="nf"><div class="form-g">
      ${fld('Ad', `<input class="inp" name="name" value="${esc(n.name)}">`)}${fld('IP / ana makine adı', `<input class="inp" name="host" value="${esc(n.host)}">`)}
      ${fld('Ajan portu', `<input class="inp" type="number" name="agent_port" value="${n.agent_port}">`)}${fld('MAC adresi (Wake-on-LAN)', `<input class="inp mono" name="mac" value="${esc(n.mac)}" placeholder="aa:bb:cc:dd:ee:ff">`)}
      ${fld('Rol', `<select class="inp" name="role">${[['laptop', 'Laptop'], ['main', 'Ana PC'], ['server', 'Sunucu']].map(([x, l]) => opt(x, l, n.role)).join('')}</select>`)}
      ${fld('Etiketler', `<input class="inp" name="tags" value="${esc((n.tags || []).join(', '))}">`, 'Virgülle; model tercihlerinde kullanılır')}
      ${fld('Hız ağırlığı', `<input class="inp" type="number" step="0.1" name="weight" value="${n.weight}">`, 'İstek dağıtımında göreli güç')}
      <div class="row">${fld('Isı uyarısı °C', `<input class="inp" type="number" name="warn_c" value="${n.warn_c}">`)}${fld('Kritik °C', `<input class="inp" type="number" name="crit_c" value="${n.crit_c}">`)}</div>
      <div class="full col">${chk('allow_sleep', 'Otomatik uykuya alınabilir', n.allow_sleep)}${chk('allow_restart', 'Otomatik yeniden başlatılabilir', n.allow_restart)}${chk('always_on', 'Her zaman açık kalsın', n.always_on)}</div></div><div class="sec">Bu makinedeki runtime yolları</div><div class="mu mb" style="font-size:12px">Boş bırakılırsa runtime'ın varsayılan yolu kullanılır. Örneğin CUDA'lı yamalı derleme bu makinede başka dizindeyse buraya yazın. Ajanın o yola izin vermesi gerekir: <code>sudo orkestra-agent-allow YOL</code></div><div class="form-g">${rts.map(x => fld(esc(x.label), `<input class="inp mono" data-rt="${esc(x.key)}" value="${esc((n.rt_paths || {})[x.key] || '')}" placeholder="${esc(x.exe)}">`)).join('')}</div></form>`,
    actions: [{ label: 'Sil', kind: 'danger', icon: 'trash', close: false, onClick: async mm => {
        if (await confirmBox(`<b>${esc(n.name)}</b> silinecek ve üzerindeki dağıtımlar kaldırılacak.`, { danger: true, ok: 'Sil' })) { await del(`/api/nodes/${id}`); toast('Sunucu silindi', 'ok'); mm.close(); location.hash = '#/nodes'; }
      } }, { label: 'Ajan bağlantısını sına', close: false, onClick: async () => { const t = await post(`/api/nodes/${id}/test`); toast(`Ajan yanıt verdi: ${t.info.hostname} (${t.info.agent})`, 'ok'); } },
      { label: 'Kaydet', kind: 'pri', onClick: async mm => {
        const rt_paths = {}; $$('[data-rt]', mm.el).forEach(i => { if (i.value.trim()) rt_paths[i.dataset.rt] = i.value.trim(); });
        await patch(`/api/nodes/${id}`, { ...formVals($('#nf', mm.el)), rt_paths }); toast('Kaydedildi', 'ok'); } }] });
}

async function nodeInstall(id) {
  const r = await api(`/api/nodes/${id}`);
  const m = modal({ title: `${esc(r.node.name)} · ajan kurulumu`, size: 'wide', body: `
    <p class="mu" style="margin-top:0">Ubuntu makinede (root/sudo ile) çalıştırın. Ajan systemd servisini, Wake-on-LAN ayarını ve güç komutları için sudo iznini kurar.</p>
    ${fld('Ajanın çalıştırmasına izin verilecek ek yollar (yamalı llama.cpp vb.)', '<input class="inp mono" id="ia" placeholder="/opt/llama-yamali/build/bin/llama-server, /opt/baska-derleme">', 'Virgülle; dosya ya da klasör (klasör yolunu / ile bitirin). İsteğe bağlı: sonradan <code>sudo orkestra-agent-allow YOL</code> ile de eklenir.')}
    <div class="copybox mt"><code id="ic"></code><button class="btn sm" id="ci">${icon('copy')}Kopyala</button></div>
    <dl class="kv mt"><dt>Ajan token'ı</dt><dd class="mono">${esc(r.agent_token)}</dd></dl>`, actions: [{ label: 'Kapat' }] });
  const cmd = () => r.install_cmd + $('#ia', m.el).value.split(',').map(x => x.trim()).filter(Boolean).map(x => (x.endsWith('/') ? ' --runtime-dir ' : ' --allow-exe ') + "'" + x.replace(/'/g, '') + "'").join('');
  const upd = () => { $('#ic', m.el).textContent = cmd(); };
  $('#ia', m.el).oninput = upd; upd();
  $('#ci', m.el).onclick = () => copyText($('#ic', m.el).textContent);
}

function addNodeWizard() {
  const token = Array.from(crypto.getRandomValues(new Uint8Array(12))).map(b => b.toString(16).padStart(2, '0')).join('');
  modal({ title: 'Elle sunucu ekle', size: 'wide', body: `<form id="an"><div class="form-g">
    ${fld('Ad', '<input class="inp" name="name" placeholder="laptop-7">')}${fld('IP adresi / ana makine adı', '<input class="inp" name="host" placeholder="192.168.1.50">', 'Sabit IP (DHCP rezervasyonu) önerilir')}
    ${fld('Rol', `<select class="inp" name="role">${[['laptop', 'Laptop'], ['server', 'Sunucu'], ['main', 'Ana PC']].map(([r, l]) => opt(r, l, 'laptop')).join('')}</select>`, 'Laptop: uykuya alınabilir · Ana PC: uyutulmaz')}
    ${fld('Etiketler', '<input class="inp" name="tags" placeholder="laptop, gpu">', 'Model tercihleri için, virgülle')}
    ${fld('Ajan portu', '<input class="inp" type="number" name="agent_port" value="7777">')}${fld('MAC adresi', '<input class="inp mono" name="mac" placeholder="boş bırakılabilir">', 'Ajan bağlanınca otomatik algılanır')}
    <div class="full">${fld('Ajan token\'ı', `<input class="inp mono" name="agent_token" value="${token}">`, 'Bu makineye özel gizli anahtar; kurulum komutuna otomatik eklenir')}</div></div></form>`,
    actions: [{ label: 'Vazgeç' }, { label: 'Ekle', kind: 'pri', onClick: async mm => {
      const r = await post('/api/nodes', formVals($('#an', mm.el)));
      location.hash = '#/nodes/' + r.node.id; setTimeout(() => nodeInstall(r.node.id), 150);
    } }] });
}
