'use strict';
/* Otomasyon: ölçekleme, güç yönetimi ve ağ geçidi ilkeleri */

const AUTO_GROUPS = [
  ['Ölçekleme', [
    ['auto.enabled', 'Otomatik ölçekleme', 'Talebe göre replika ekler, boşta kalanı kaldırır', 'sw'],
    ['auto.dry_run', 'Yalnızca öner', 'Kararlar günlüğe yazılır, uygulanmaz', 'sw'],
    ['auto.target_concurrency', 'Replika başına hedef eşzamanlı istek', 'Yük bunu aşınca yeni replika eklenir', 'num'],
    ['auto.scale_up_cooldown_s', 'Ölçek artırma bekleme süresi (sn)', '', 'num'],
    ['auto.interval_s', 'Değerlendirme aralığı (sn)', '', 'num']]],
  ['Güç yönetimi', [
    ['auto.allow_wake', 'Uyuyan sunucuları uyandır', 'Gerekince Wake-on-LAN ile', 'sw'],
    ['auto.allow_sleep', 'Boştaki sunucuları uykuya al', '', 'sw'],
    ['auto.sleep_after_idle_min', 'Uykudan önce boşta kalma süresi (dk)', '', 'num'],
    ['auto.min_awake_nodes', 'En az açık kalacak sunucu sayısı', '', 'num'],
    ['auto.wake_timeout_s', 'Uyandırma zaman aşımı (sn)', '', 'num'],
    ['auto.allow_reboot', 'Arızada otomatik yeniden başlat', 'Örneğin GPU kaybında', 'sw'],
    ['auto.max_reboots_per_hour', 'Sunucu başına saatte en çok yeniden başlatma', '', 'num']]],
  ['Ağ geçidi', [
    ['gw.cold_start_timeout_s', 'Soğuk başlatma bekleme sınırı (sn)', 'Model yüklenirken istek bekletilir', 'num'],
    ['gw.queue_timeout_s', 'Kuyruk bekleme sınırı (sn)', '', 'num'],
    ['gw.max_queue', 'En fazla kuyruk uzunluğu', '', 'num'],
    ['gw.affinity', 'Konuşma yakınlığı', 'Aynı konuşma aynı replikaya gider (önbellek verimi)', 'sw'],
    ['gw.log_chats', 'Sohbetleri kaydet', '', 'sw']]],
];
const MODES = [['performance', 'Performans'], ['balanced', 'Dengeli'], ['eco', 'Tasarruf']];
const MODE_HINT = { performance: 'İşi güçlü sunuculara yayar, hız önceliklidir.', balanced: 'Boştaki güçlü sunucuyu tercih eder, gereksiz uyandırmaz.', eco: 'Az sayıda sunucuda toplar, geri kalanı uykuya alır.' };

App.register('auto', {
  title: 'Otomasyon',
  mount(el) {
    el.innerHTML = `<div class="cmd"><button class="cb" id="au-tick">${icon('refresh')}Şimdi değerlendir</button><span class="grow"></span><span class="mu" id="au-last"></span></div>
      <div class="g2" style="align-items:start;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr)"><div class="card"><div class="card-h"><h3>İlkeler</h3></div><div class="card-b" id="au-form"></div></div>
      <div class="col" style="gap:12px"><div class="card"><div class="card-h"><h3>Model hedefleri</h3></div><div class="card-b np" id="au-models"></div></div>
      <div class="card"><div class="card-h"><h3>Karar günlüğü</h3></div><div class="card-b np"><div class="feed" id="au-log" style="max-height:430px;overflow:auto"></div></div></div></div></div>`;
    $('#au-tick').onclick = async () => { try { await post('/api/autoscaler/tick'); toast('Değerlendirildi', 'ok'); this.status(); } catch (e) { toastErr(e); } };
    api('/api/settings').then(s => this.form(s)).catch(toastErr);
    const save = async (k, v) => { try { await put('/api/settings', { [k]: v }); toast('Kaydedildi', 'ok', 1200); } catch (e) { toastErr(e); } };
    on(el, 'change', '#au-form [data-k]', t => save(t.dataset.k, t.type === 'checkbox' ? t.checked : t.value));
    on(el, 'click', '#au-form [data-mode]', b => { $$('#au-form [data-mode]').forEach(x => x.classList.toggle('on', x === b)); $('#au-mh').textContent = MODE_HINT[b.dataset.mode]; save('auto.mode', b.dataset.mode); });
    App.every(() => this.status(), 3000);
  },
  form(s) {
    $('#au-form').innerHTML = `<div class="set-row"><div class="t"><b>Strateji</b><small id="au-mh">${MODE_HINT[s['auto.mode']] || ''}</small></div><div class="seg">${MODES.map(([k, l]) => `<button data-mode="${k}" class="${k === s['auto.mode'] ? 'on' : ''}">${l}</button>`).join('')}</div></div>` +
      AUTO_GROUPS.map(([g, rows]) => `<div class="sec">${g}</div>` + rows.map(([k, t, h, ty]) => `<div class="set-row"><div class="t"><b>${t}</b>${h ? `<small>${h}</small>` : ''}</div>${ty === 'sw' ? sw('', s[k]).replace('name=""', `data-k="${k}"`) : `<input class="inp" type="number" data-k="${k}" value="${s[k]}">`}</div>`).join('')).join('');
  },
  async status() {
    const a = await api('/api/autoscaler');
    if (!$('#au-log')) return;
    $('#au-last').textContent = a.last_tick ? `son değerlendirme ${fmt.ago(a.last_tick)}${a.enabled ? '' : ' · otomasyon kapalı'}` : 'henüz çalışmadı';
    $('#au-models').innerHTML = `<table class="tbl"><thead><tr><th>Model</th><th class="num">İstenen</th><th class="num">Var</th><th class="num">Hazır</th><th class="num">Yük</th><th class="num">İstek/dk</th></tr></thead><tbody>` +
      (a.models.map(m => `<tr><td><b>${esc(m.name)}</b></td><td class="num">${m.wanted}</td><td class="num">${m.have}</td><td class="num">${m.ready}</td><td class="num">${m.load}</td><td class="num">${m.rpm}</td></tr>`).join('') || `<tr><td colspan="6">${emptyBox('Henüz değerlendirme yok')}</td></tr>`) + '</tbody></table>' +
      (a.unmet.length ? `<div class="alert" style="margin:10px">${icon('alert')}<div>${a.unmet.map(u => `<b>${esc(u.model)}</b>: ${esc(u.reason)} (${fmt.ago(u.since)})`).join('<br>')}</div></div>` : '');
    const kinds = { scale_up: ['info', 'ekle'], scale_down: ['', 'kaldır'], sleep: ['', 'uyut'], reboot: ['err', 'yeniden başlat'], thermal: ['err', 'ısı'], recover: ['warn', 'kurtar'] };
    $('#au-log').innerHTML = a.decisions.map(d => `<div class="it"><time>${fmt.time(d.ts)}</time>${sq((kinds[d.kind] || [''])[0])}<div class="m"><b>${esc(d.label)}</b> <span class="mu">— ${esc(d.reason)}</span>${d.executed ? '' : ' <span class="tag">öneri</span>'}${d.error ? ` <span class="err">${esc(d.error)}</span>` : ''}</div></div>`).join('') || emptyBox('Henüz karar yok', 'Talep geldikçe ve sunucular boşta kaldıkça burada görünür.');
  },
});
