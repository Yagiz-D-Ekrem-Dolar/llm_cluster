'use strict';
/* Site bağlantısı: hosting'deki siteye servis, servis adları, paketler ve kullanım */

const RELAYS = [['php', 'PHP (paylaşımlı hosting)', 'relay.php dosyasını sitenizin altına yükleyin, ör. public_html/llm/relay.php. Adres: https://siteniz.com/llm/relay.php'],
  ['node', 'Node.js uygulaması', 'Zip içeriğini Node.js web uygulaması olarak yayınlayın (başlatma: npm start). Adres: uygulamanın alan adı.'],
  ['python', 'Python (VPS)', 'python3 relay.py --host 127.0.0.1 --port 8088 çalıştırın, önüne HTTPS koyun. Adres: yönlendirdiğiniz alan adı.']];
const LINK_MODES = [['relay', 'Hosting üzerinde röle', 'Önerilen. Küme hosting\'e kendisi bağlanır; modemde port açmak gerekmez.'],
  ['cloudflare', 'Cloudflare Tunnel', 'cloudflared bu bilgisayarda kuruluysa. Token girilmezse geçici adres alınır.'],
  ['direct', 'Doğrudan (port yönlendirme)', 'Yalnızca modemde gerçek IP varsa. Mobil hatlarda çoğunlukla çalışmaz.'], ['off', 'Kapalı', 'Site bağlantısı yok.']];

function serviceDialog(s, models) {
  const v = s || { name: '', kind: 'llm', targets: [], is_default: false, enabled: true, options: {} }, opts = v.options || {};
  const dlg = modal({ title: s ? `${esc(s.name)} · servis` : 'Servis ekle', size: 'wide', body: `<form id="sv"><div class="form-g">
    ${fld('Sitenin kullanacağı ad', `<input class="inp mono" name="name" value="${esc(v.name)}" ${s ? 'readonly' : ''} placeholder="sohbet">`, 'API isteğindeki model alanı. Arkadaki modeli değiştirseniz de site aynı adı kullanır.')}
    ${fld('Tür', `<select class="inp" name="kind" ${s ? 'disabled' : ''}>${Object.entries(KINDS).map(([k, l]) => opt(k, l, v.kind)).join('')}</select>`)}
    <div class="full">${fld('Modeller (sıra öncelik demektir, ilki hazır değilse sonraki kullanılır)', '<div class="card" id="sv-m" style="max-height:170px;overflow:auto"></div>')}</div>
    <div id="sv-o" class="full form-g"></div>
    <div class="full col">${chk('is_default', 'İstekte model adı yoksa bu servis kullanılsın', v.is_default)}${chk('enabled', 'Etkin', v.enabled)}</div></div></form>`,
    actions: [s && { label: 'Sil', kind: 'danger', icon: 'trash', close: false, onClick: async mm => { if (await confirmBox(`<b>${esc(s.name)}</b> servisi silinecek; siteniz bu adı kullanıyorsa istekler hata verir.`, { danger: true, ok: 'Sil' })) { await del('/api/services/' + encodeURIComponent(s.name)); mm.close(); App.cur.load?.(); } } },
      { label: 'Vazgeç' }, { label: 'Kaydet', kind: 'pri', onClick: async mm => {
        const f = formVals($('#sv', mm.el)), kind = s ? s.kind : f.kind, options = {};
        ['language', 'voice', 'response_format'].forEach(k => { if (f['o_' + k]) options[k] = f['o_' + k]; });
        await put('/api/services/' + encodeURIComponent(f.name.trim()), { kind, targets: picked(), is_default: f.is_default, enabled: f.enabled, options });
        toast('Kaydedildi', 'ok'); App.cur.load?.();
      } }].filter(Boolean) });
  let order = [...v.targets];
  const picked = () => order;
  const kindNow = () => s ? s.kind : $('[name=kind]', dlg.el).value;
  const draw = () => {
    const k = kindNow(), ms = models.filter(m => m.kind === k);
    order = order.filter(n => ms.some(m => m.name === n));
    const sorted = [...order.map(n => ms.find(m => m.name === n)), ...ms.filter(m => !order.includes(m.name))];
    $('#sv-m', dlg.el).innerHTML = sorted.map(m => { const i = order.indexOf(m.name); return `<div class="pick"><input type="checkbox" data-n="${esc(m.name)}" ${i >= 0 ? 'checked' : ''}><b>${i >= 0 ? i + 1 + '. ' : ''}${esc(m.name)}</b><span class="mu">${m.ready} hazır · ${esc(m.runtime_label)}</span><span class="grow"></span>${i > 0 ? `<button type="button" class="btn sm" data-up="${esc(m.name)}">Yukarı</button>` : ''}</div>`; }).join('') || emptyBox('Bu türde model yok', 'Modeller sayfasından ekleyin.');
    $('#sv-o', dlg.el).innerHTML = k === 'stt' ? fld('Varsayılan dil', `<select class="inp" name="o_language">${[['', 'Otomatik'], ['tr', 'Türkçe'], ['en', 'İngilizce']].map(([a, b]) => opt(a, b, opts.language || '')).join('')}</select>`)
      : k === 'tts' ? fld('Varsayılan ses', `<input class="inp" name="o_voice" value="${esc(opts.voice || '')}" placeholder="runtime varsayılanı">`) + fld('Ses biçimi', `<select class="inp" name="o_response_format">${['', 'wav', 'mp3', 'opus', 'flac'].map(x => opt(x, x || 'istemci seçer', opts.response_format || '')).join('')}</select>`) : '';
  };
  on($('#sv-m', dlg.el), 'change', '[data-n]', c => { const n = c.dataset.n; order = c.checked ? [...order, n] : order.filter(x => x !== n); draw(); });
  on($('#sv-m', dlg.el), 'click', '[data-up]', b => { const i = order.indexOf(b.dataset.up); if (i > 0) { [order[i - 1], order[i]] = [order[i], order[i - 1]]; draw(); } });
  if (!s) $('[name=kind]', dlg.el).onchange = draw;
  draw();
}

App.register('site', {
  title: 'Site bağlantısı', tab: 'conn', u: null, sv: [], us: null, ms: [], st: null, code: 'php', gs: false,
  mount(el) {
    this.u = null; this.gs = false;
    el.innerHTML = `<div class="strip" id="s-strip"></div>
      <div class="tabs">${[['conn', 'Bağlantı'], ['svc', 'Servisler'], ['plan', 'Paketler ve kullanım'], ['code', 'Site entegrasyonu']].map(([k, l]) => `<button class="tab ${k === this.tab ? 'on' : ''}" data-t="${k}">${l}</button>`).join('')}</div><div id="s-body"></div>`;
    on(el, 'click', '.tab', b => { this.tab = b.dataset.t; this.gs = false; $$('.tab', el).forEach(x => x.classList.toggle('on', x === b)); $('#s-body').innerHTML = ''; this.load(true); });
    on(el, 'click', '[data-sv]', b => serviceDialog(this.sv.find(x => x.name === b.dataset.sv), this.ms));
    on(el, 'click', '#s-add', () => serviceDialog(null, this.ms));
    on(el, 'click', '[data-code]', b => { this.code = b.dataset.code; this.drawCode(); });
    on(el, 'click', '[data-copy]', b => copyText($(b.dataset.copy).textContent));
    App.every(() => this.load(), 3000);
  },
  async load(force) {
    const [u, sv] = await Promise.all([api('/api/uplink'), api('/api/services')]); this.u = u; this.sv = sv;
    if (force || !this.ms.length || this.tab === 'svc') this.ms = await api('/api/models');
    if (this.tab === 'plan') { [this.us, this.st] = await Promise.all([api('/api/usage?days=30'), this.st && !force ? this.st : api('/api/settings')]); }
    if (this.tab === 'code' && !this.keys) this.keys = await api('/api/keys');
    this.render(force);
  },
  render(force) {
    const u = this.u, sb = $('#s-body'); if (!u || !sb) return;
    const label = { up: ['Bağlı', 'ok'], connecting: ['Bağlanıyor', 'warn'], down: ['Kopuk', 'err'], error: ['Hata', 'err'], off: ['Kapalı', 'idle'] }[u.state] || [u.state, 'idle'];
    const ready = this.sv.filter(s => s.ready > 0).length;
    $('#s-strip').innerHTML = [['Bağlantı', badge(label[0], label[1])], ['Dış adres', u.base_url ? `<span class="mono" style="font-size:13px">${esc(u.base_url)}</span>` : '—'], ['Gecikme', u.rtt_ms != null && u.state === 'up' ? `${u.rtt_ms}<small>ms</small>` : '—'],
      ['Açık istek', u.active], ['Toplam istek', fmt.n(u.total)], ['Hazır servis', `${ready}<small>/ ${this.sv.length}</small>`]].map(([k, v]) => `<div class="si"><span class="k">${k}</span><div class="v">${v}</div></div>`).join('');
    if (this.tab === 'conn') this.drawConn(force);
    else if (this.tab === 'svc') this.drawSvc();
    else if (this.tab === 'plan') this.drawPlan(force);
    else this.drawCode();
  },
  drawConn() {
    const u = this.u, c = u.config, b = $('#s-body');
    if (!$('#c-f')) {
      b.innerHTML = `<div class="g2" style="align-items:start"><div class="card"><div class="card-h"><h3>Bağlantı yöntemi</h3></div><div class="card-b"><form id="c-f" class="col">
          ${LINK_MODES.map(([k, l, t]) => `<label class="rd"><input type="radio" name="mode" value="${k}" ${c.mode === k ? 'checked' : ''}><span><b>${l}</b><small>${t}</small></span></label>`).join('')}
          <div id="c-x"></div><div class="row"><button class="btn pri" type="submit">Kaydet ve bağlan</button><button class="btn" type="button" id="c-test">${icon('activity')}Uçtan uca sına</button></div><div id="c-res"></div></form></div></div>
        <div class="col"><div id="s-g1"></div><div id="s-g2"></div><div id="s-g3"></div><div class="alert info" style="margin:0">${icon('info')}<div>Superbox gibi mobil modemler çoğunlukla dışarıdan erişime kapalıdır (CGNAT). Röle ve Cloudflare yönteminde bağlantıyı küme başlattığı için modem ayarı gerekmez.</div></div></div></div>`;
      const extra = () => {
        const m = $('[name=mode]:checked', b).value, x = $('#c-x', b);
        x.innerHTML = m === 'relay' ? `<div class="sec" style="margin-top:0">1. Röle dosyasını indirip hosting'e yükleyin</div>${RELAYS.map(([k, l, t]) => `<div class="dl"><a class="btn sm" href="/api/uplink/relay/${k}">${icon('download')}${l}</a><small>${t}</small></div>`).join('')}<div class="mu" style="font-size:12px">Dosya tünel anahtarını içerir; yalnızca kendi sunucunuza yükleyin.</div><div class="sec">2. Rölenin adresini yazın</div>${fld('Röle adresi', `<input class="inp mono" name="relay_url" value="${esc(c.relay_url)}" placeholder="https://siteniz.com/llm/relay.php">`)}`
          : m === 'cloudflare' ? `<div class="form-g">${fld('Tünel token\'ı', `<input class="inp mono" type="password" name="cf_token" placeholder="${c.has_cf_token ? 'kayıtlı' : 'boş: geçici adres'}">`)}${fld('cloudflared yolu', `<input class="inp mono" name="cf_path" value="${esc(c.cf_path)}">`)}<div class="full">${fld('Genel adres', `<input class="inp mono" name="public_url" value="${esc(c.public_url)}" placeholder="https://api.siteniz.com">`)}</div></div>`
            : m === 'direct' ? fld('Genel adres', `<input class="inp mono" name="public_url" value="${esc(c.public_url)}" placeholder="https://api.siteniz.com">`, 'Bu adres ağ geçidi portuna yönlendirilmiş olmalı') : '';
      };
      on(b, 'change', '[name=mode]', extra); extra();
      $('#c-f').onsubmit = async e => { e.preventDefault(); try { const v = formVals($('#c-f')); this.u = await put('/api/uplink', { mode: v.mode, relay_url: v.relay_url || c.relay_url, public_url: v.public_url ?? c.public_url, cf_token: v.cf_token, cf_path: v.cf_path || c.cf_path }); toast('Kaydedildi', 'ok'); this.render(); } catch (er) { toastErr(er); } };
      $('#c-test').onclick = async () => { const r = $('#c-res'); r.innerHTML = '<span class="mu">Sınanıyor…</span>'; try { const t = await post('/api/uplink/test'); r.innerHTML = `<div class="card">${t.steps.map(s => `<div class="tr">${sq(s.ok ? 'ok' : 'err')}<span class="grow">${esc(s.name)}</span><span class="mu">${esc(s.note)}</span><b>${fmt.ms(s.ms)}</b></div>`).join('')}</div>`; } catch (e) { r.innerHTML = `<div class="alert err" style="margin:0">${icon('alert')}<div>${esc(e.message)}</div></div>`; } };
      const mk = (id, label, color, f, key, extra2 = {}) => new Graph($('#' + id), { label, color, f, max: null, auto: 10, win: 900, grid: 60, gap: 30, fresh: 30, cls: 'mini2', ...extra2, src: () => { const h = this.u?.hist; return h ? { t: h.t, lines: key(h) } : null; } });
      mk('s-g1', 'Tünel gecikmesi', GC.net, v => Math.round(v) + ' ms', h => [{ v: h.rtt }], { auto: 100 });
      mk('s-g2', 'Açık istek', GC.req, v => String(Math.round(v)), h => [{ v: h.active }], { auto: 4 });
      mk('s-g3', 'Tünel trafiği', GC.cpu, fmt.bps, h => [{ v: h.up, name: 'siteye' }, { v: h.down, name: 'siteden', color: '#a15814', fill: false }], { auto: 50000 });
    }
    const err = u.error ? `<div class="alert err" style="margin:10px 0 0">${icon('alert')}<div>${esc(u.error)}</div></div>` : '';
    let box = $('#c-err'); if (!box) { box = document.createElement('div'); box.id = 'c-err'; $('#c-res').before(box); } box.innerHTML = err;
  },
  drawSvc() {
    const b = $('#s-body');
    b.innerHTML = `<div class="cmd"><button class="cb" id="s-add">${icon('plus')}Servis ekle</button><span class="grow"></span><span class="mu">Site bu adları kullanır; arkadaki modeli buradan değiştirirsiniz</span></div>
      <div class="card"><div class="tbl-w"><table class="tbl"><thead><tr><th>Servis adı</th><th>Tür</th><th>Modeller</th><th class="num">Hazır replika</th><th class="num">Çalışan / kapasite</th><th class="num">Kuyruk</th><th></th></tr></thead><tbody>` +
      (this.sv.map(s => `<tr><td><b class="mono">${esc(s.name)}</b>${s.is_default ? ' <span class="tag">varsayılan</span>' : ''}${s.enabled ? '' : ' <span class="tag">kapalı</span>'}</td><td>${esc(KINDS[s.kind])}</td><td>${s.models.map((m, i) => `<span class="mchip">${sq(m.ready ? 'ok' : m.missing ? 'err' : '')}${i + 1}. ${esc(m.name)}</span>`).join(' ')}${s.ready ? '' : `<span class="sub warn">Hazır replika yok: ilk istek modelin yüklenmesini bekler. Modelde “en az 1 replika” açın.</span>`}</td><td class="num">${s.ready}</td><td class="num">${s.inflight} / ${s.capacity}</td><td class="num">${s.queue || 0}</td><td class="act"><button class="btn sm" data-sv="${esc(s.name)}">Düzenle</button></td></tr>`).join('') || `<tr><td colspan="7">${emptyBox('Servis yok', 'Sohbet, konuşma → metin ve metin → konuşma için birer servis tanımlayın.')}</td></tr>`) + '</tbody></table></div></div>';
  },
  drawPlan(force) {
    const b = $('#s-body'), us = this.us, plans = this.st?.['svc.plans'] || {};
    if (!us) return;
    if (!$('#p-f') || force) {
      b.innerHTML = `<div class="g3 mb"><div id="u-g1"></div><div id="u-g2"></div><div id="u-g3"></div></div>
        <div class="g2" style="align-items:start"><div class="card"><div class="card-h"><h3>Paketler</h3><div class="sp mu">Site isteğe X-User-Plan başlığıyla paket adını ekler</div></div><form id="p-f"><div class="tbl-w"><table class="tbl"><thead><tr><th>Paket</th><th class="num">Sıra önceliği</th><th class="num">İstek/dk</th><th class="num">Eşzamanlı</th><th class="num">Günlük token</th><th class="num">Yanıt sınırı</th></tr></thead><tbody>${Object.entries(plans).map(([k, p]) => `<tr data-p="${esc(k)}"><td><b>${esc(p.label || k)}</b><span class="sub mono">${esc(k)}</span></td>${['rank', 'rpm', 'max_conc', 'day_tokens', 'max_tokens'].map(f => `<td class="num"><input class="inp cell" type="number" min="0" data-f="${f}" value="${p[f] ?? 0}"></td>`).join('')}</tr>`).join('')}</tbody></table></div><div class="card-b row"><button class="btn pri" type="submit">Kaydet</button><span class="mu">0 = sınırsız · sıra önceliğinde küçük olan önce çalışır</span></div></form></div>
        <div class="card"><div class="card-h"><h3>En çok kullananlar · 30 gün</h3></div><div class="tbl-w" id="u-top" style="max-height:300px"></div></div></div>`;
      $('#p-f').onsubmit = async e => { e.preventDefault(); const out = JSON.parse(JSON.stringify(plans)); $$('[data-p]', b).forEach(r => $$('[data-f]', r).forEach(i => { out[r.dataset.p][i.dataset.f] = +i.value || 0; })); try { await put('/api/settings', { 'svc.plans': out }); this.st = null; toast('Kaydedildi', 'ok'); } catch (er) { toastErr(er); } };
      const day = d => new Date(d + 'T12:00:00').getTime() / 1000;
      const mk = (id, label, color, f, pick) => new Graph($('#' + id), { label, color, f, max: null, auto: 10, win: 30 * 86400, grid: 7 * 86400, gap: 200000, fresh: 200000, dayfmt: true, cls: 'mid', src: () => { const d = this.us?.days || []; return { t: d.map(x => day(x.day)), lines: [{ v: d.map(pick) }] }; } });
      mk('u-g1', 'İstek / gün', GC.req, v => fmt.n(v), x => x.requests); mk('u-g2', 'Token / gün', GC.gpu, v => fmt.n(v), x => x.in_tok + x.out_tok); mk('u-g3', 'Aktif kullanıcı', GC.mem, v => fmt.n(v), x => x.users);
    }
    $('#u-top').innerHTML = `<table class="tbl"><thead><tr><th>Kullanıcı</th><th>Paket</th><th class="num">İstek</th><th class="num">Token</th><th class="num">Ses</th></tr></thead><tbody>${us.users.map(u => `<tr><td class="mono ell" style="max-width:190px">${esc(u.user_id)}</td><td>${esc(u.plan || '—')}</td><td class="num">${fmt.n(u.requests)}</td><td class="num">${fmt.n(u.in_tok + u.out_tok)}</td><td class="num">${u.audio_s ? fmt.dur(u.audio_s) : '—'}</td></tr>`).join('') || `<tr><td colspan="5">${emptyBox('Henüz kullanım yok')}</td></tr>`}</tbody></table>`;
  },
  drawCode() {
    const b = $('#s-body'), base = (this.u.base_url || 'https://ADRES') + '/v1', def = k => (this.sv.find(s => s.kind === k && s.is_default) || this.sv.find(s => s.kind === k))?.name || 'MODEL';
    const chat = def('llm'), stt = def('stt'), tts = def('tts');
    const S = {
      php: ['PHP', `<?php // sohbet.php: tarayıcı buraya yazar, anahtar sunucunuzda kalır
$KEY = getenv('LLM_KEY');
header('Content-Type: text/event-stream'); header('X-Accel-Buffering: no');
$in = json_decode(file_get_contents('php://input'), true);
$ch = curl_init('${base}/chat/completions');
curl_setopt_array($ch, [
  CURLOPT_POST => true,
  CURLOPT_HTTPHEADER => ["Authorization: Bearer $KEY", 'Content-Type: application/json',
    'X-User-Id: ' . $kullanici_id, 'X-User-Plan: ' . $paket, 'X-Conversation-Id: ' . $in['sohbet_id']],
  CURLOPT_POSTFIELDS => json_encode(['model' => '${chat}', 'stream' => true, 'messages' => $in['messages']]),
  CURLOPT_WRITEFUNCTION => function ($c, $d) { echo $d; flush(); return strlen($d); },
]);
curl_exec($ch);`],
      node: ['Node.js', `// Express: anahtar sunucuda kalır, yanıt akış olarak tarayıcıya geçer
app.post('/api/sohbet', async (req, res) => {
  const iptal = new AbortController();
  res.on('close', () => iptal.abort());   // kullanıcı ayrılırsa GPU boşa çalışmasın
  const r = await fetch('${base}/chat/completions', {
    method: 'POST', signal: iptal.signal,
    headers: { Authorization: 'Bearer ' + process.env.LLM_KEY, 'Content-Type': 'application/json',
      'X-User-Id': req.user.id, 'X-User-Plan': req.user.plan, 'X-Conversation-Id': req.body.sohbetId },
    body: JSON.stringify({ model: '${chat}', stream: true, messages: req.body.messages }),
  });
  res.status(r.status).set('Content-Type', 'text/event-stream');
  for await (const parca of r.body) res.write(parca);
  res.end();
});`],
      voice: ['Ses', `# Konuşma → metin
curl ${base}/audio/transcriptions -H "Authorization: Bearer ANAHTAR" -H "X-User-Id: kullanici-1" \\
  -F model=${stt} -F file=@kayit.webm

# Metin → konuşma (ses parça parça gelir)
curl ${base}/audio/speech -H "Authorization: Bearer ANAHTAR" -H "X-User-Id: kullanici-1" \\
  -H "Content-Type: application/json" -d '{"model":"${tts}","input":"Merhaba"}' -o ses.wav`],
      token: ['Tarayıcı jetonu', `// Tarayıcının doğrudan bağlanması için kısa ömürlü jeton (sunucunuzda üretin, imza anahtarı gizli kalır)
import crypto from 'node:crypto';
const b64 = x => Buffer.from(x).toString('base64url');
function kullaniciJetonu(anahtarNo, imzaAnahtari, kullanici, paket) {
  const yuk = b64(JSON.stringify({ k: anahtarNo, u: kullanici, p: paket, exp: Math.floor(Date.now() / 1000) + 3600 }));
  return 'ut1.' + yuk + '.' + crypto.createHmac('sha256', imzaAnahtari).update(yuk).digest('base64url');
}
// Tarayıcı: Authorization: Bearer <jeton>. Ayarlar → API anahtarları'nda site adresinizi izinli yapın.`],
    };
    if (!S[this.code]) this.code = 'php';
    b.innerHTML = `<div class="g2" style="grid-template-columns:minmax(0,300px) minmax(0,1fr);align-items:start"><div class="card"><div class="card-h"><h3>Bağlantı bilgileri</h3></div><div class="card-b"><dl class="kv kv2"><dt>Adres</dt><dd class="mono">${esc(base)}</dd><dt>Sohbet</dt><dd class="mono">${esc(chat)}</dd><dt>Konuşma → metin</dt><dd class="mono">${esc(stt)}</dd><dt>Metin → konuşma</dt><dd class="mono">${esc(tts)}</dd></dl>
      <div class="mt"><a class="btn sm" href="#/settings">${icon('key')}API anahtarı oluştur</a></div><div class="mu mt" style="font-size:12px">OpenAI uyumludur. Kullanım: <code>GET /v1/usage?user=ID</code> · durum: <code>GET /v1/status</code></div></div></div>
      <div class="card"><div class="tabs" style="margin:0;padding:4px 8px 0">${Object.entries(S).map(([k, [l]]) => `<button class="tab ${k === this.code ? 'on' : ''}" data-code="${k}">${l}</button>`).join('')}<span class="grow"></span><button class="btn sm" data-copy="#c-src" style="margin:0 0 4px">${icon('copy')}Kopyala</button></div><pre class="log" id="c-src" style="border:0;max-height:440px">${esc(S[this.code][1])}</pre></div></div>`;
  },
});
