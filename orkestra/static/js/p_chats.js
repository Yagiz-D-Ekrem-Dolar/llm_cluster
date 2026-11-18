'use strict';
/* Sohbet kayıtları: örnek sohbetler, konuşmalar, kullanıcılar */

async function openConversation(id) {
  let c;
  try { c = await api('/api/conversations/' + encodeURIComponent(id)); } catch (e) { return toastErr(e); }
  const tl = c.turn_list || [];
  modal({ title: esc(c.title || c.id), size: 'wide', body: `
    <div class="row wrap mb mu"><span>Kullanıcı <b class="tx2">${esc(c.user_id)}</b></span><span>·</span><span>${esc(c.model || '')}</span><span>·</span><span>${c.turns} tur</span><span>·</span><span>${fmt.dt(c.last_ts)}</span><span>·</span><span class="mono">${esc(c.id)}</span></div>
    <div class="col" style="gap:8px;background:#fff;border:1px solid var(--line2);padding:12px;max-height:46vh;overflow:auto">${(c.messages || []).map(m => `<div class="msg ${m.role === 'user' ? 'user' : m.role === 'system' ? 'system' : 'assistant'}">${esc(typeof m.content === 'string' ? m.content : JSON.stringify(m.content))}</div>`).join('') || emptyBox('İçerik yok')}</div>
    <div class="tbl-w mt card"><table class="tbl"><thead><tr><th>Tur</th><th>Saat</th><th>Sunucu</th><th class="num">Giriş / çıkış jeton</th><th class="num">İlk token</th><th class="num">Toplam</th><th>Durum</th></tr></thead><tbody>${tl.map((t, i) => `<tr><td>${i + 1}</td><td>${fmt.time(t.ts)}</td><td>${esc(t.node || '—')}</td><td class="num">${t.in_tok ?? '—'} / ${t.out_tok ?? '—'}</td><td class="num">${fmt.ms(t.ttft_ms)}</td><td class="num">${fmt.ms(t.total_ms)}</td><td>${badge(t.status === 'ok' ? 'tamam' : t.status, t.status === 'ok' ? 'ok' : 'err')}</td></tr>`).join('')}</tbody></table></div>`,
    actions: [{ label: 'Sil', kind: 'danger', icon: 'trash', close: false, onClick: async mm => { if (await confirmBox('Bu konuşma kaydı silinecek.', { danger: true, ok: 'Sil' })) { await del('/api/conversations/' + encodeURIComponent(id)); mm.close(); toast('Silindi', 'ok'); } } }, { label: 'Kapat' }] });
}

App.register('chats', {
  title: 'Sohbet kayıtları', tab: 'sample', user: '', q: '', model: '',
  mount(el) {
    el.innerHTML = `<div class="cmd"><span class="mu" id="ct-stat" style="padding:0 8px"></span><span class="grow"></span><button class="cb" id="ct-exp">${icon('download')}JSONL dışa aktar</button></div>
      <div class="tabs">${[['sample', 'Örnek sohbetler'], ['convs', 'Konuşmalar'], ['users', 'Kullanıcılar']].map(([k, l]) => `<button class="tab ${k === this.tab ? 'on' : ''}" data-t="${k}">${l}</button>`).join('')}</div><div id="ct-body"></div>`;
    $('#ct-exp').onclick = () => window.open('/api/export/chats.jsonl?anonymize=1');
    on(el, 'click', '.tab', b => { this.tab = b.dataset.t; $$('.tab', el).forEach(x => x.classList.toggle('on', x === b)); this.load(); });
    on(el, 'click', '[data-conv]', t => openConversation(t.dataset.conv));
    on(el, 'click', '[data-user]', t => { this.user = t.dataset.user; this.tab = 'convs'; $$('.tab', el).forEach(x => x.classList.toggle('on', x.dataset.t === 'convs')); this.load(); });
    api('/api/chats/stats').then(s => { $('#ct-stat').textContent = `${fmt.n(s.users)} kullanıcı · ${fmt.n(s.conversations)} konuşma · ${fmt.n(s.turns)} tur · son 24 saatte ${fmt.n(s.today_turns)} tur · ${fmt.n(s.pending_export)} konuşma HuggingFace'e gönderilmeyi bekliyor`; }).catch(() => { });
    this.load(); App.every(() => this.tab === 'convs' || this.tab === 'users' ? this.load(true) : null, 5000);
  },
  async load(quiet) {
    const b = $('#ct-body'); if (!b) return;
    try {
      if (this.tab === 'sample') {
        const cs = await api('/api/conversations?sort=random&limit=12');
        b.innerHTML = `<div class="row mb"><button class="btn" id="ct-shuf">${icon('shuffle')}Karıştır</button><span class="mu">Rastgele ${cs.length} konuşma</span></div><div class="g3">` +
          (cs.map(c => `<div class="conv-card" data-conv="${esc(c.id)}"><div class="q">${esc(c.title || '(başlıksız)')}</div><div class="mu">${esc(c.user_id)} · ${esc(c.model || '')}</div><div class="mu">${c.turns} tur · ${fmt.ago(c.last_ts)}</div></div>`).join('') || emptyBox('Henüz kayıtlı sohbet yok')) + '</div>';
        $('#ct-shuf').onclick = () => this.load();
      } else if (this.tab === 'convs') {
        if (!quiet || !$('#ct-q')) {
          b.innerHTML = `<div class="row mb"><input class="inp" id="ct-q" placeholder="Başlıkta ara…" style="max-width:240px" value="${esc(this.q)}"><input class="inp" id="ct-u" placeholder="Kullanıcı" style="max-width:200px" value="${esc(this.user)}"><button class="btn" id="ct-go">Ara</button></div><div class="card"><div class="tbl-w" id="ct-list"></div></div>`;
          $('#ct-go').onclick = () => { this.q = $('#ct-q').value; this.user = $('#ct-u').value; this.load(true); };
          $('#ct-q').onkeydown = $('#ct-u').onkeydown = e => { if (e.key === 'Enter') $('#ct-go').click(); };
        }
        const cs = await api(`/api/conversations?limit=80&q=${encodeURIComponent(this.q)}&user=${encodeURIComponent(this.user)}`);
        $('#ct-list').innerHTML = `<table class="tbl"><thead><tr><th>Konu</th><th>Kullanıcı</th><th>Model</th><th class="num">Tur</th><th>Son etkinlik</th><th>Kaynak</th></tr></thead><tbody>` +
          (cs.map(c => `<tr class="click" data-conv="${esc(c.id)}"><td class="ell" style="max-width:380px">${esc(c.title || '(başlıksız)')}</td><td>${esc(c.user_id)}</td><td>${esc(c.model || '')}</td><td class="num">${c.turns}</td><td class="nw">${fmt.dt(c.last_ts)}</td><td>${esc(c.source || '')}</td></tr>`).join('') || `<tr><td colspan="6">${emptyBox('Kayıt bulunamadı')}</td></tr>`) + '</tbody></table>';
      } else {
        const us = await api('/api/users?limit=200');
        b.innerHTML = `<div class="card"><div class="tbl-w"><table class="tbl"><thead><tr><th>Kullanıcı</th><th class="num">Tur</th><th class="num">Jeton</th><th>İlk görülme</th><th>Son görülme</th></tr></thead><tbody>` +
          (us.map(u => `<tr class="click" data-user="${esc(u.id)}"><td><b>${esc(u.id)}</b></td><td class="num">${fmt.n(u.turns)}</td><td class="num">${fmt.n(u.tokens)}</td><td class="nw">${fmt.dt(u.first_seen)}</td><td class="nw">${fmt.dt(u.last_seen)}</td></tr>`).join('') || `<tr><td colspan="5">${emptyBox('Henüz kullanıcı yok')}</td></tr>`) + '</tbody></table></div></div>';
      }
    } catch (e) { if (!quiet) toastErr(e); }
  },
});
