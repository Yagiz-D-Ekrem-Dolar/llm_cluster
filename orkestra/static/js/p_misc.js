'use strict';
/* Olaylar ve ayarlar */

App.register('events', {
  title: 'Olaylar', level: '', source: '',
  mount(el) {
    el.innerHTML = `<div class="cmd"><span class="mu" style="padding:0 6px">Düzey</span><select class="inp" id="ev-l" style="width:130px"><option value="">hepsi</option><option value="error">hata</option><option value="warn">uyarı</option><option value="info">bilgi</option></select>
      <span class="mu" style="padding:0 6px">Kaynak</span><select class="inp" id="ev-s" style="width:150px"><option value="">hepsi</option>${['node', 'power', 'deploy', 'fabric', 'uplink', 'autoscaler', 'thermal', 'model', 'distill', 'hf', 'sim'].map(x => `<option>${x}</option>`).join('')}</select><span class="grow"></span><span class="mu" id="ev-n"></span></div>
      <div class="card"><div class="tbl-w" id="ev-list"></div></div>`;
    $('#ev-l').onchange = e => { this.level = e.target.value; this.load(); };
    $('#ev-s').onchange = e => { this.source = e.target.value; this.load(); };
    App.every(() => this.load(), 3000);
  },
  async load() {
    const ev = await api(`/api/events?limit=300&level=${this.level}&source=${this.source}`); if (!$('#ev-list')) return;
    $('#ev-n').textContent = ev.length + ' olay';
    $('#ev-list').innerHTML = `<table class="tbl"><thead><tr><th style="width:150px">Zaman</th><th style="width:90px">Düzey</th><th style="width:100px">Kaynak</th><th>Olay</th></tr></thead><tbody>` +
      (ev.map(e => `<tr><td class="nw">${new Date(e.ts * 1000).toLocaleString('tr-TR')}</td><td>${badge({ error: 'hata', warn: 'uyarı', info: 'bilgi' }[e.level] || e.level, { error: 'err', warn: 'warn', info: 'idle' }[e.level])}</td><td class="mu">${esc(e.source)}</td><td>${esc(e.message)}</td></tr>`).join('') || `<tr><td colspan="4">${emptyBox('Olay yok')}</td></tr>`) + '</tbody></table>';
  },
});

App.register('settings', {
  title: 'Ayarlar', tab: 'keys',
  mount(el) {
    const tabs = [['keys', 'API anahtarları'], ['dist', 'Dağıtım'], ['sys', 'Sistem'], ['sec', 'Güvenlik']];
    if (App.authState.demo) tabs.push(['sim', 'Simülatör']);
    el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button class="tab ${k === this.tab ? 'on' : ''}" data-t="${k}">${l}</button>`).join('')}</div><div id="st-body"></div>`;
    on(el, 'click', '.tab', b => { this.tab = b.dataset.t; $$('.tab', el).forEach(x => x.classList.toggle('on', x === b)); this.draw(); });
    this.draw();
  },
  destroy() { clearInterval(this.simTimer); },
  async draw() {
    const old = $('#st-body'); if (!old) return; clearInterval(this.simTimer);
    const b = old.cloneNode(false); old.replaceWith(b);
    try {
      if (this.tab === 'keys') return this.keys(b);
      if (this.tab === 'dist') return this.dist(b);
      if (this.tab === 'sys') return this.sys(b);
      if (this.tab === 'sec') return this.sec(b);
      return this.sim(b);
    } catch (e) { toastErr(e); }
  },
  async keys(b) {
    const ks = await api('/api/keys');
    b.innerHTML = `<div class="cmd"><button class="cb" id="k-new">${icon('plus')}Anahtar oluştur</button><span class="grow"></span><span class="mu">Siteniz ağ geçidine bu anahtarlarla bağlanır</span></div>
      <div class="card"><div class="tbl-w"><table class="tbl"><thead><tr><th>Ad</th><th>Anahtar</th><th class="num">İstek/dk</th><th>Modeller</th><th>Sohbet kaydı</th><th>Oluşturma</th><th>Son kullanım</th><th></th></tr></thead><tbody>` +
      (ks.map(k => `<tr><td><b>${esc(k.name)}</b></td><td class="mono">${esc(k.prefix)}…</td><td class="num">${k.rpm || '∞'}</td><td>${k.models.length ? chips(k.models) : '<span class="mu">hepsi</span>'}</td><td>${k.log_chats ? 'evet' : 'hayır'}</td><td class="nw">${fmt.dt(k.created_at)}</td><td class="nw">${k.last_used_at ? fmt.ago(k.last_used_at) : '—'}</td>
        <td class="act">${k.revoked ? badge('iptal', 'idle') : `<button class="btn sm danger" data-rev="${k.id}">İptal et</button>`}</td></tr>`).join('') || `<tr><td colspan="8">${emptyBox('API anahtarı yok', 'Sitenizin arka ucu için bir anahtar oluşturun.')}</td></tr>`) + '</tbody></table></div></div>';
    $('#k-new').onclick = () => modal({ title: 'API anahtarı oluştur', body: `<form id="kf">${fld('Ad', '<input class="inp" name="name" placeholder="site">')}<div class="form-g mt">${fld('Dakikalık istek sınırı', '<input class="inp" type="number" name="rpm" value="0">', '0 = sınırsız; kullanıcı sınırları pakette')}${fld('İzinli modeller', '<input class="inp" name="models" placeholder="boş = hepsi">', 'Virgülle')}</div><div class="mt">${chk('log_chats', 'Bu anahtarla gelen sohbetleri kaydet', true)}</div></form>`,
      actions: [{ label: 'Vazgeç' }, { label: 'Oluştur', kind: 'pri', onClick: async mm => {
        const v = formVals($('#kf', mm.el)), r = await post('/api/keys', { name: v.name, rpm: v.rpm, models: v.models.split(',').map(x => x.trim()).filter(Boolean), log_chats: v.log_chats });
        const m2 = modal({ title: 'Anahtar oluşturuldu', body: `<div class="alert">${icon('alert')}<div>Bu anahtar yalnızca <b>bir kez</b> gösterilir. Şimdi kopyalayıp güvenli bir yere kaydedin.</div></div>${fld('API anahtarı', `<div class="copybox"><code>${esc(r.key)}</code><button type="button" class="btn sm" id="kc">${icon('copy')}Kopyala</button></div>`)}<div class="mt">${fld(`İmza anahtarı (anahtar no ${r.id}, tarayıcı jetonları için)`, `<div class="copybox"><code>${esc(r.sign_secret)}</code><button type="button" class="btn sm" id="ks">${icon('copy')}Kopyala</button></div>`)}</div>`, actions: [{ label: 'Tamam' }] });
        $('#ks', m2.el).onclick = () => copyText(r.sign_secret);
        $('#kc', m2.el).onclick = () => copyText(r.key); this.draw();
      } }] });
    on(b, 'click', '[data-rev]', async t => { if (await confirmBox('Bu anahtar iptal edilecek; onu kullanan uygulamalar erişemez.', { danger: true, ok: 'İptal et' })) { await del(`/api/keys/${t.dataset.rev}`); this.draw(); } });
  },
  async dist(b) {
    const st = await api('/api/settings');
    const row = (k, t, h, unit = '') => `<div class="set-row"><div class="t"><b>${t}</b><small>${h}</small></div><input class="inp" type="number" min="0" step="any" name="${k}" value="${st[k]}">${unit ? `<span class="mu">${unit}</span>` : ''}</div>`;
    const tog = (k, t, h) => `<div class="set-row"><div class="t"><b>${t}</b><small>${h}</small></div>${sw(k, st[k])}</div>`;
    b.innerHTML = `<div class="card" style="max-width:720px"><div class="card-h"><h3>Dağıtım ve çalıştırma</h3></div><div class="card-b"><form id="df">
      ${row('fabric.hf_limit_mbps', 'İnternetten indirme sınırı', 'Model indirirken modem dolup site yavaşlamasın. 0 = sınırsız', 'Mbit/sn')}${row('fabric.lan_limit_mbps', 'Switch aktarım sınırı', 'Makine başına. 0 = sınırsız', 'Mbit/sn')}
      ${row('fabric.source_slots', 'Ana PC eşzamanlı gönderim', 'Makineler birbirinden de alır; ana PC darboğaz olmaz', 'aktarım')}${row('fabric.reserve_gb', 'Makinelerde boş bırakılacak disk', 'Yer azalınca kullanılmayan modeller silinir', 'GB')}
      ${tog('fabric.preseed', 'Modelleri önceden dağıt', 'İndirilen model, çalıştırabilecek makinelere boşta kopyalanır; talep gelince beklenmez')}${tog('fabric.auto_update_agent', 'Ajanları otomatik güncelle', 'Çalışan çıkarım süreçleri kesilmez')}
      ${tog('deploy.smoke_test', 'Trafikten önce deneme çıkarımı', 'Model gerçek yanıt üretmeden kullanıcıya açılmaz')}${tog('deploy.avoid_battery', 'Pille çalışan makineye iş verme', 'Şarj kablosu çıkmış laptoplar atlanır')}
      <div class="row mt"><button class="btn pri" type="submit">Kaydet</button></div></form></div></div>`;
    $('#df').onsubmit = async e => { e.preventDefault(); try { await put('/api/settings', formVals($('#df'))); toast('Kaydedildi', 'ok'); } catch (er) { toastErr(er); } };
  },
  async sys(b) {
    const [s, st] = await Promise.all([api('/api/system'), api('/api/settings')]);
    b.innerHTML = `<div class="g2" style="align-items:start"><div class="card"><div class="card-h"><h3>Sistem</h3></div><div class="card-b"><dl class="kv">
      <dt>Sürüm</dt><dd>${esc(s.version)}</dd><dt>Python</dt><dd>${esc(s.python)}</dd><dt>Platform</dt><dd>${esc(s.platform)}</dd><dt>Çalışma süresi</dt><dd>${fmt.dur(s.uptime_s)}</dd>
      <dt>Veri klasörü</dt><dd class="mono" style="font-size:11.5px">${esc(s.data_dir)}</dd><dt>Veritabanı</dt><dd>${s.db_mb} MB</dd><dt>Panel portu</dt><dd>${s.admin_port}</dd><dt>Genel ağ geçidi</dt><dd>${s.public_port ? 'port ' + s.public_port : 'kapalı'}</dd>
      <dt>huggingface_hub</dt><dd>${s.hf_hub ? badge('kurulu', 'ok') : badge('kurulu değil (pip install huggingface_hub)', 'warn')}</dd></dl></div></div>
      <div class="card"><div class="card-h"><h3>Ağ ve saklama</h3></div><div class="card-b"><form id="sf">
        ${fld('Kontrol sunucusu adresi', `<input class="inp mono" name="net.controller_url" value="${esc(st['net.controller_url'])}" placeholder="${esc(s.controller_url)}">`, 'Sunuculara verilen kurulum komutunda kullanılır. Boşsa otomatik: ' + esc(s.controller_url))}
        <div class="mt">${fld('Modem adresi', `<input class="inp mono" name="net.modem_ip" value="${esc(st['net.modem_ip'])}" placeholder="otomatik (varsayılan ağ geçidi)">`)}</div>
        <div class="mt">${fld('Sitenin adresleri (tarayıcıdan doğrudan bağlantı için)', `<input class="inp mono" name="gw.cors_origins" value="${esc((st['gw.cors_origins'] || []).join(', '))}" placeholder="https://siteniz.com">`, 'Virgülle. Yalnızca tarayıcı jetonu kullanıyorsanız gerekir.')}</div>
        <div class="mt">${fld('Metrik ve olay saklama süresi (gün)', `<input class="inp" type="number" name="sys.retention_days" value="${st['sys.retention_days']}">`)}</div>
        <div class="row mt"><button class="btn pri" type="submit">Kaydet</button></div></form></div></div></div>`;
    $('#sf').onsubmit = async e => { e.preventDefault(); try { await put('/api/settings', formVals($('#sf'))); toast('Kaydedildi', 'ok'); } catch (er) { toastErr(er); } };
  },
  sec(b) {
    b.innerHTML = `<div class="card" style="max-width:480px"><div class="card-h"><h3>Yönetici parolası</h3></div><div class="card-b"><form id="pf" class="col">
      ${fld('Mevcut parola', '<input class="inp" type="password" name="current" autocomplete="current-password">')}${fld('Yeni parola', '<input class="inp" type="password" name="new" autocomplete="new-password">', 'En az 8 karakter')}
      <div><button class="btn pri" type="submit">Parolayı değiştir</button></div></form></div></div>
      <div class="alert info mt" style="max-width:480px">${icon('info')}<div>Panel yalnızca yerel ağınızda açık olmalıdır. Site bağlantısı için modemde port açmanız gerekmez.</div></div>`;
    $('#pf').onsubmit = async e => {
      e.preventDefault(); const v = formVals($('#pf'));
      try { await post('/api/settings/password', v); toast('Parola değişti; yeniden giriş yapın', 'ok'); setTimeout(() => App.showAuth('login'), 800); } catch (er) { toastErr(er); }
    };
  },
  async sim(b) {
    const draw = async () => {
      const [s, ov] = await Promise.all([api('/api/sim/state'), api('/api/overview')]); if (!$('#sm-n')) return;
      $('#sm-t').innerHTML = s.traffic.enabled ? `${icon('pause')}Demo trafiğini durdur` : `${icon('play')}Demo trafiğini başlat`;
      $('#sm-r').textContent = s.traffic.enabled ? `${s.traffic.rpm} istek/dk` : 'durdu';
      $('#sm-n').innerHTML = s.nodes.map(n => `<tr><td><b>${esc(n.name)}</b></td><td>${esc(n.state)}${n.bias ? ' · aşırı ısınma' : ''}${n.gpu_lost ? ' · GPU kayıp' : ''}</td>
        <td class="act">${[['overheat', 'Aşırı ısıt'], ['crash', 'Süreç çökert'], ['gpu_lost', 'GPU kaybet'], ['offline', 'Fişi çek'], ['recover', 'Düzelt']].map(([k, l]) => `<button class="btn sm ${k === 'recover' ? '' : 'danger'}" data-inj="${k}" data-id="${n.id}">${l}</button>`).join(' ')}</td></tr>`).join('');
    };
    b.innerHTML = `<div class="alert info">${icon('info')}<div>Demo modu: sunucular ve çıkarım simüle edilir. Arıza enjekte ederek otomatik uyku, uyandırma, yeniden başlatma ve ısı korumasını deneyebilirsiniz.</div></div>
      <div class="cmd"><button class="cb" id="sm-t"></button><span class="mu" id="sm-r"></span><span class="sep"></span><span class="mu">Süre ölçeği</span><select class="inp" id="sm-s" style="width:90px"><option value="1">1×</option><option value="3">3×</option><option value="6">6×</option></select></div>
      <div class="card"><div class="tbl-w"><table class="tbl"><thead><tr><th>Simüle sunucu</th><th>Durum</th><th>Arıza enjekte et</th></tr></thead><tbody id="sm-n"></tbody></table></div></div>`;
    $('#sm-t').onclick = async () => { const s = await api('/api/sim/state'); await post('/api/sim/traffic', { enabled: !s.traffic.enabled }); draw(); };
    $('#sm-s').onchange = e => post('/api/sim/speed', { time_scale: +e.target.value });
    on(b, 'click', '[data-inj]', async t => { try { await post('/api/sim/inject', { node_id: +t.dataset.id, kind: t.dataset.inj }); toast('Uygulandı', 'ok'); draw(); } catch (e) { toastErr(e); } });
    await draw(); this.simTimer = setInterval(draw, 3000);
  },
});
