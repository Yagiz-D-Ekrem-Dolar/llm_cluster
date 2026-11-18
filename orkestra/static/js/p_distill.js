'use strict';
/* Distillation ve HuggingFace senkronu */

const JOB_ST = { queued: ['Sırada', 'info'], running: ['Çalışıyor', 'ok'], paused: ['Duraklatıldı', 'idle'], done: ['Bitti', 'ok'], cancelled: ['İptal', 'idle'], error: ['Hata', 'err'] };

function distillSettings(s, models) {
  const llm = models.filter(m => m.kind === 'llm' && m.enabled);
  const row = (k, t, h, c) => `<div class="set-row"><div class="t"><b>${t}</b>${h ? `<small>${h}</small>` : ''}</div>${c}</div>`;
  modal({ title: 'Otomatik distillation ayarları', size: 'wide', body: `<form id="dc">
    ${row('', 'Otomatik mod', 'Küme boştayken günün sorularını öğretmen modele damıtır', sw('distill.enabled', s['distill.enabled']))}
    ${row('', 'Gün içinde sorulanları kullan', 'Sohbet kayıtlarındaki tekil kullanıcı soruları', sw('distill.use_daily', s['distill.use_daily']))}
    ${row('', 'Küme boşta kalma süresi (dk)', 'Bu süre istek gelmezse başlar, istek gelince duraklar', `<input class="inp" type="number" name="distill.idle_min" value="${s['distill.idle_min']}">`)}
    ${row('', 'Günlük en çok soru', '', `<input class="inp" type="number" name="distill.daily_limit" value="${s['distill.daily_limit']}">`)}
    ${row('', 'Uyuyan sunucuyu uyandırabilir', 'Kapalıysa yalnızca açık sunucuların boş kapasitesi kullanılır', sw('distill.allow_wake', s['distill.allow_wake']))}
    <div class="sec">Öğretmen model</div><div class="form-g">
      ${fld('Yerel model', `<select class="inp" name="distill.teacher"><option value="">(seçilmedi)</option>${llm.map(m => opt(m.name, m.name, s['distill.teacher'])).join('')}</select>`)}
      ${fld('Sıcaklık', `<input class="inp" type="number" step="0.1" name="distill.temperature" value="${s['distill.temperature']}">`)}
      ${fld('En fazla jeton', `<input class="inp" type="number" name="distill.max_tokens" value="${s['distill.max_tokens']}">`)}${fld('Eşzamanlı istek', `<input class="inp" type="number" name="distill.concurrency" value="${s['distill.concurrency']}">`)}
      <div class="full">${fld('Sistem istemi', `<textarea class="inp" name="distill.system" rows="2">${esc(s['distill.system'])}</textarea>`)}</div></div>
    <div class="sec">Harici öğretmen (isteğe bağlı)</div><div class="form-g">
      ${fld('OpenAI uyumlu adres', `<input class="inp mono" name="distill.teacher_url" value="${esc(s['distill.teacher_url'])}" placeholder="https://api.example.com/v1">`, 'Doluysa ve yerel model seçilmediyse bu kullanılır')}
      ${fld('Model adı', `<input class="inp" name="distill.teacher_model" value="${esc(s['distill.teacher_model'])}">`)}
      <div class="full">${fld('API anahtarı', `<input class="inp mono" type="password" name="distill.teacher_key" placeholder="${s['distill.teacher_key'] ? '•••••••• (kayıtlı; değiştirmek için yazın)' : ''}" autocomplete="off">`)}</div></div></form>`,
    actions: [{ label: 'Vazgeç' }, { label: 'Kaydet', kind: 'pri', onClick: async mm => { await put('/api/settings', formVals($('#dc', mm.el))); toast('Kaydedildi', 'ok'); } }] });
}

App.register('distill', {
  title: 'Distillation', d: null, models: [],
  mount(el) {
    el.innerHTML = `<div class="cmd"><button class="cb" id="di-set">${icon('plus')}Soru listesi ekle</button><button class="cb" id="di-job">${icon('flask')}Yeni iş</button><span class="sep"></span><button class="cb" id="di-cfg">${icon('gear')}Otomatik mod ayarları…</button></div>
      <div class="strip" id="di-strip"></div>
      <div class="sec">Soru listeleri</div><div class="card"><div class="tbl-w" id="di-sets"></div></div>
      <div class="sec">İşler</div><div class="card"><div class="tbl-w" id="di-jobs"></div></div>`;
    api('/api/models').then(m => { this.models = m; }).catch(() => { });
    $('#di-set').onclick = () => this.addSet();
    $('#di-job').onclick = () => this.newJob();
    $('#di-cfg').onclick = async () => distillSettings(await api('/api/settings'), this.models);
    on(el, 'click', '[data-j]', async b => {
      const id = +b.dataset.id, a = b.dataset.j;
      try {
        if (a === 'items') return this.items(id);
        if (a === 'delset') { if (await confirmBox('Bu soru listesi silinecek.', { danger: true, ok: 'Sil' })) await del(`/api/distill/sets/${id}`); }
        else if (a === 'delete' || a === 'cancel') { if (!await confirmBox(a === 'delete' ? 'İş ve üretilen yanıtlar silinecek.' : 'İş iptal edilecek.', { danger: true, ok: 'Tamam' })) return; await post(`/api/distill/jobs/${id}/action`, { action: a }); }
        else await post(`/api/distill/jobs/${id}/action`, { action: a });
        this.load();
      } catch (e) { toastErr(e); }
    });
    App.every(() => this.load(), 3000);
  },
  async load() {
    const d = this.d = await api('/api/distill/status'); if (!$('#di-strip')) return;
    const stT = { running: 'çalışıyor', waiting: 'bekliyor', idle: 'boşta' }[d.state] || d.state;
    $('#di-strip').innerHTML = [['Durum', d.enabled ? stT : 'otomatik kapalı'], ['Bekleyen soru', fmt.n(d.pending)], ['Üretilen yanıt', fmt.n(d.done)], ['Gönderilmeyi bekleyen', fmt.n(d.unexported)], ['Küme boşta', `${fmt.n(d.idle_min, 1)}<small>dk</small>`]]
      .map(([k, v]) => `<div class="si"><span class="k">${k}</span><div class="v">${v}</div></div>`).join('') + (d.reason ? `<div class="si grow"><span class="k">Neden bekliyor</span><div class="v" style="font-size:13px;font-weight:400;padding-top:3px">${esc(d.reason)}</div></div>` : '');
    $('#di-sets').innerHTML = `<table class="tbl"><thead><tr><th>Liste</th><th class="num">Soru</th><th>Eklenme</th><th></th></tr></thead><tbody>` + (d.sets.map(s => `<tr><td><b>${esc(s.name)}</b></td><td class="num">${s.count}</td><td>${fmt.dt(s.created_at)}</td><td class="act"><button class="btn sm danger" data-j="delset" data-id="${s.id}">Sil</button></td></tr>`).join('') || `<tr><td colspan="4">${emptyBox('Soru listesi yok', 'Satır başına bir soru içeren bir metin ya da JSONL ekleyin.')}</td></tr>`) + '</tbody></table>';
    $('#di-jobs').innerHTML = `<table class="tbl"><thead><tr><th>İş</th><th>Durum</th><th style="min-width:170px">İlerleme</th><th>Öğretmen</th><th>Oluşturma</th><th></th></tr></thead><tbody>` + (d.jobs.map(j => {
      const [t, c] = JOB_ST[j.status] || [j.status, 'idle'], pct = j.total ? (j.done + j.failed) / j.total * 100 : 0, cf = j.config || {};
      return `<tr><td><b>${esc(j.name)}</b><span class="sub">${cf.forced ? 'hemen' : 'boştayken'}${j.error ? ' · ' + esc(j.error) : ''}</span></td><td>${badge(t, c)}</td>
        <td>${j.done}/${j.total}${j.failed ? ` <span class="err">(${j.failed} hata)</span>` : ''}${bar(pct, j.status === 'done' ? 'ok' : '')}</td><td>${esc(cf.teacher || '(varsayılan)')}</td><td class="nw">${fmt.dt(j.created_at)}</td>
        <td class="act"><button class="btn sm" data-j="items" data-id="${j.id}">Örnekler</button>
          ${['queued', 'paused', 'error'].includes(j.status) ? `<button class="btn sm" data-j="start" data-id="${j.id}">Başlat</button>` : ''}${['queued', 'running'].includes(j.status) ? `<button class="btn sm" data-j="pause" data-id="${j.id}">Duraklat</button>` : ''}
          ${!['done', 'cancelled'].includes(j.status) ? `<button class="btn sm" data-j="cancel" data-id="${j.id}">İptal</button>` : ''}<button class="btn sm danger" data-j="delete" data-id="${j.id}">Sil</button></td></tr>`;
    }).join('') || `<tr><td colspan="6">${emptyBox('Henüz iş yok')}</td></tr>`) + '</tbody></table>';
  },
  addSet() {
    modal({ title: 'Soru listesi ekle', size: 'wide', body: `<form id="ds">${fld('Ad', '<input class="inp" name="name" placeholder="Genel kültür soruları">')}
      <div class="mt">${fld('Sorular', '<textarea class="inp mono" name="text" rows="12" placeholder="Satır başına bir soru.\nYa da JSONL: {&quot;prompt&quot;: &quot;...&quot;}"></textarea>')}</div>
      <div class="row mt"><input type="file" id="ds-file" accept=".txt,.jsonl,.json,.csv"><span class="mu">Dosya seçerseniz içeriği yukarı aktarılır</span></div></form>`,
      actions: [{ label: 'Vazgeç' }, { label: 'Ekle', kind: 'pri', onClick: async mm => { const r = await post('/api/distill/sets', formVals($('#ds', mm.el))); toast('Liste eklendi', 'ok'); this.load(); return r; } }] });
    $('#ds-file').onchange = async e => { const f = e.target.files[0]; if (f) { $('#ds textarea').value = await f.text(); const n = $('#ds [name=name]'); if (!n.value) n.value = f.name.replace(/\.\w+$/, ''); } };
  },
  newJob() {
    const d = this.d || { sets: [] }, llm = this.models.filter(m => m.kind === 'llm' && m.enabled);
    modal({ title: 'Yeni distillation işi', size: 'wide', body: `<form id="dj"><div class="form-g">${fld('İş adı', '<input class="inp" name="name" placeholder="Haftalık damıtma">')}
      ${fld('Öğretmen model', `<select class="inp" name="teacher"><option value="">(ayarlardaki varsayılan)</option>${llm.map(m => opt(m.name, m.name, '')).join('')}</select>`)}
      <div class="full"><span class="mu" style="font-weight:600;font-size:12px">Soru kaynakları</span><div class="col mt" style="gap:6px">${d.sets.map(s => chk('set_' + s.id, `${esc(s.name)} <span class="mu">(${s.count} soru)</span>`, false)).join('') || '<span class="mu">Önce bir soru listesi ekleyin ya da yalnızca günlük soruları kullanın.</span>'}
        <div class="row">${chk('use_daily', 'Gün içinde sorulanlar', true)}<span class="mu">son</span><input class="inp" type="number" name="daily_hours" value="24" style="width:70px"><span class="mu">saat</span></div></div></div>
      ${fld('En çok soru', '<input class="inp" type="number" name="limit" placeholder="sınırsız">')}${fld('Eşzamanlı istek', '<input class="inp" type="number" name="concurrency" placeholder="varsayılan">')}
      ${fld('En fazla jeton', '<input class="inp" type="number" name="max_tokens" placeholder="varsayılan">')}${fld('Sıcaklık', '<input class="inp" type="number" step="0.1" name="temperature" placeholder="varsayılan">')}
      <div class="full">${fld('Çalışma zamanı', '<select class="inp" name="forced"><option value="1">Hemen başlat (düşük öncelikle; gerçek isteklerin önüne geçmez)</option><option value="0">Yalnızca küme boştayken çalıştır</option></select>')}</div></div></form>`,
      actions: [{ label: 'Vazgeç' }, { label: 'Oluştur', kind: 'pri', onClick: async mm => {
        const v = formVals($('#dj', mm.el)), sets = Object.keys(v).filter(k => k.startsWith('set_') && v[k]).map(k => +k.slice(4));
        await post('/api/distill/jobs', { name: v.name, teacher: v.teacher, sets, use_daily: v.use_daily, daily_hours: v.daily_hours || 24, limit: v.limit, concurrency: v.concurrency, max_tokens: v.max_tokens, temperature: v.temperature, forced: v.forced === '1' });
        toast('İş oluşturuldu', 'ok'); this.load();
      } }] });
  },
  async items(id) {
    const rows = await api(`/api/distill/jobs/${id}/items?limit=60`);
    modal({ title: `Örnekler · iş ${id}`, size: 'xl', body: `<div class="col" style="gap:8px">${rows.map(r => `<div class="card"><div class="card-h"><b class="ell" style="max-width:720px">${esc(r.prompt)}</b><div class="sp">${badge(r.status, r.status === 'done' ? 'ok' : r.status === 'failed' ? 'err' : 'warn')}</div></div>
      <div class="card-b" style="white-space:pre-wrap">${r.response ? esc(r.response) : `<span class="mu">${esc(r.error || 'henüz yanıt yok')}</span>`}</div></div>`).join('') || emptyBox('Henüz örnek yok')}</div>`, actions: [{ label: 'Kapat' }] });
  },
});

App.register('hf', {
  title: 'HuggingFace',
  mount(el) {
    el.innerHTML = `<div class="cmd"><button class="cb" id="hf-save">${icon('check')}Kaydet</button><button class="cb" id="hf-test">${icon('info')}Bağlantıyı sına</button><button class="cb" id="hf-sync">${icon('upload')}Şimdi gönder</button><span class="grow"></span><span class="mu" id="hf-next"></span></div>
      <div class="g2" style="align-items:start"><div class="card"><div class="card-h"><h3>Hedef ve zamanlama</h3></div><div class="card-b" id="hf-form"></div></div>
      <div class="col" style="gap:12px"><div class="card"><div class="card-h"><h3>Durum</h3></div><div class="card-b" id="hf-stat"></div></div><div class="card"><div class="card-h"><h3>Gönderim geçmişi</h3></div><div class="card-b np" id="hf-hist"></div></div></div></div>`;
    api('/api/settings').then(s => this.form(s)).catch(toastErr);
    $('#hf-save').onclick = () => this.save();
    $('#hf-test').onclick = async () => { try { const r = await post('/api/hf/test'); r.ok ? toast(`Bağlandı: ${r.user}`, 'ok') : toast(r.error, 'err', 7000); } catch (e) { toastErr(e); } };
    $('#hf-sync').onclick = async () => { try { await this.save(true); const r = await post('/api/hf/sync'); toast(`${r.status}: ${r.records} kayıt — ${r.message}`, r.status === 'error' ? 'err' : 'ok', 7000); this.status(); } catch (e) { toastErr(e); } };
    on(el, 'click', '#hf-clr', async () => { await post('/api/settings/clear', { key: 'hf.token' }); toast('Token silindi', 'ok'); $('#hf-tok').placeholder = ''; });
    App.every(() => this.status(), 4000);
  },
  form(s) {
    const row = (t, h, c) => `<div class="set-row"><div class="t"><b>${t}</b>${h ? `<small>${h}</small>` : ''}</div>${c}</div>`;
    $('#hf-form').innerHTML = `<form id="hff"><div class="form-g">
      <div class="full">${fld('Erişim token\'ı (yazma izni)', `<div class="row"><input class="inp mono" id="hf-tok" name="hf.token" type="password" autocomplete="off" placeholder="${s['hf.token'] ? '•••••••• (kayıtlı; değiştirmek için yazın)' : 'hf_…'}"><button type="button" class="btn" id="hf-clr">Sil</button></div>`)}</div>
      ${fld('Sohbet veri seti deposu', `<input class="inp mono" name="hf.chat_repo" value="${esc(s['hf.chat_repo'])}" placeholder="kullanici/sohbet-kayitlari">`)}
      ${fld('Damıtılmış veri seti deposu', `<input class="inp mono" name="hf.distill_repo" value="${esc(s['hf.distill_repo'])}" placeholder="boşsa yukarıdakiyle aynı">`)}
      <div class="full">${fld('Gönderim saatleri', `<input class="inp mono" name="hf.schedule" value="${esc((s['hf.schedule'] || []).join(', '))}" placeholder="03:00, 15:00">`, 'Sunucunun yerel saati; virgülle ayırın')}</div></div>
      <div class="mt">${row('Zamanlanmış gönderim', '', sw('hf.auto', s['hf.auto']))}${row('Deneme modu (dry-run)', 'Dosyalar hazırlanır ama gönderilmez; yalnızca yerel klasörde kalır', sw('hf.dry_run', s['hf.dry_run']))}
      ${row('Depo özel olsun', 'Yoksa özel (private) oluşturulur', sw('hf.private', s['hf.private']))}${row('Kullanıcı kimliklerini anonimleştir', 'Geri döndürülemez özet (hash) yazılır', sw('hf.anonymize', s['hf.anonymize']))}
      ${row('E-posta, telefon, IBAN, T.C. kimlik no maskele', '', sw('hf.scrub_pii', s['hf.scrub_pii']))}${row('Konuşma bitmiş sayılma süresi (dk)', 'Bu süre sessiz kalan konuşmalar gönderilir', `<input class="inp" type="number" name="hf.idle_min" value="${s['hf.idle_min']}">`)}</div></form>`;
  },
  async save(quiet) {
    const v = formVals($('#hff'));
    if (!v['hf.token']) delete v['hf.token'];
    await put('/api/settings', v);
    if (!quiet) toast('Kaydedildi', 'ok');
  },
  async status() {
    const s = await api('/api/hf/status'); if (!$('#hf-stat')) return;
    $('#hf-next').textContent = s.auto ? 'sonraki gönderim: ' + s.next_run : 'zamanlama kapalı';
    $('#hf-stat').innerHTML = `<dl class="kv"><dt>Token</dt><dd>${s.token_set ? badge('kayıtlı', 'ok') : badge('yok', 'warn')}</dd><dt>Mod</dt><dd>${s.dry_run ? badge('deneme (dry-run)', 'warn') : badge('canlı gönderim', 'ok')}</dd>
      <dt>Bekleyen sohbet</dt><dd>${fmt.n(s.pending_conversations)} konuşma</dd><dt>Bekleyen damıtma</dt><dd>${fmt.n(s.pending_distill)} çift</dd><dt>Hazırlanan dosyalar</dt><dd class="mono" style="font-size:11.5px">${s.outbox_files.map(esc).join('<br>') || '—'}</dd><dt>Yerel klasör</dt><dd class="mono" style="font-size:11.5px">${esc(s.outbox)}</dd></dl>`;
    $('#hf-hist').innerHTML = `<table class="tbl"><thead><tr><th>Zaman</th><th>Tetikleyen</th><th>Sonuç</th><th class="num">Kayıt</th></tr></thead><tbody>` + (s.history.map(h => `<tr><td class="nw">${fmt.dt(h.ts)}</td><td>${esc(h.trigger)}</td><td>${badge(h.status, h.status === 'ok' ? 'ok' : h.status === 'error' ? 'err' : 'warn')}<span class="sub" title="${esc(h.message)}">${esc((h.message || '').slice(0, 70))}</span></td><td class="num">${h.records}</td></tr>`).join('') || `<tr><td colspan="4">${emptyBox('Henüz gönderim yok')}</td></tr>`) + '</tbody></table>';
  },
});
