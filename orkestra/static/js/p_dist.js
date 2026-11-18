'use strict';
/* Dağıtım: modeller ve runtime'ların ana PC'den makinelere aktarımı */

App.register('dist', {
  title: 'Dağıtım', d: null,
  mount(el) {
    this.d = null;
    el.innerHTML = `<div class="cmd"><button class="cb" id="d-all">${icon('upload')}Tüm modelleri uygun makinelere dağıt</button><button class="cb" id="d-agent">${icon('refresh')}Ajanları güncelle</button><span class="grow"></span><span class="mu" id="d-note"></span></div>
      <div class="strip" id="d-strip"></div><div class="sec">Modeller ve makineler</div><div class="card"><div class="tbl-w" id="d-mx"></div></div>
      <div class="sec">Süren işler</div><div class="card"><div id="d-tr"></div></div>
      <div class="sec">Makine depoları</div><div class="card"><div class="tbl-w" id="d-nodes"></div></div>`;
    on(el, 'click', '[data-act]', async b => {
      const id = +b.dataset.id, a = b.dataset.act;
      try {
        if (a === 'dl') { await post(`/api/fabric/models/${id}/download`); toast('İndirme başladı', 'ok'); }
        else if (a === 'push') { const r = await post(`/api/fabric/models/${id}/push`, {}); toast(r.queued ? `${r.queued} aktarım sıraya alındı` : 'Tüm makinelerde mevcut', 'ok'); }
        else if (a === 'refresh') { await post(`/api/fabric/models/${id}/refresh`); toast('Dosya listesi güncellendi', 'ok'); }
        else if (a === 'cell') { await post(`/api/fabric/models/${id}/push`, { node_ids: [+b.dataset.n] }); toast('Aktarım sıraya alındı', 'ok'); }
        else if (a === 'agent') { await post(`/api/fabric/nodes/${id}/update-agent`); toast('Ajan güncelleniyor', 'ok'); }
        this.load();
      } catch (e) { toastErr(e); }
    });
    $('#d-all').onclick = async () => { let n = 0; for (const m of this.d?.models || []) { if (!m.total) continue; if (m.in_store < m.total) await post(`/api/fabric/models/${m.id}/download`).catch(toastErr); else { const r = await post(`/api/fabric/models/${m.id}/push`, {}).catch(() => ({})); n += r.queued || 0; } } toast(n ? `${n} aktarım sıraya alındı` : 'İşlem başlatıldı', 'ok'); this.load(); };
    $('#d-agent').onclick = async () => { for (const n of (this.d?.nodes || []).filter(x => x.status === 'online' && !x.agent_ok)) await post(`/api/fabric/nodes/${n.id}/update-agent`).catch(toastErr); toast('Eski ajanlar güncelleniyor', 'ok'); };
    App.every(() => this.load(), 2000);
  },
  async load() { this.d = await api('/api/fabric'); this.render(); },
  render() {
    const d = this.d; if (!d || !$('#d-mx')) return;
    const act = d.transfers.filter(x => x.state === 'active'), bps = act.reduce((a, x) => a + x.bps, 0), dls = d.models.filter(m => m.download?.state === 'running');
    const cells = [['Ana PC deposu', `${fmt.size(d.store.bytes)}<small>${d.store.files} dosya</small>`], ['Boş disk', fmt.size(d.store.free_bytes)], ['Süren aktarım', `${act.length}<small>${fmt.bps(bps)}</small>`], ['Sırada', d.transfers.filter(x => x.state === 'queued').length],
      ['Modemden inen', fmt.size(d.wan_down)], ['Switch üzerinden', fmt.size(d.lan_bytes)]];
    $('#d-strip').innerHTML = cells.map(([k, v]) => `<div class="si"><span class="k">${k}</span><div class="v">${v}</div></div>`).join('');
    $('#d-note').textContent = dls.length ? `${dls.length} model internetten iniyor` : 'Model dosyaları modemden yalnızca bir kez iner';
    const ns = d.nodes;
    $('#d-mx').innerHTML = `<table class="tbl mx"><thead><tr><th>Model</th><th>Ana PC</th>${ns.map(n => `<th class="c" title="${esc(n.status)}">${esc(n.name)}</th>`).join('')}<th></th></tr></thead><tbody>` + (d.models.map(m => {
      const t = m.download, sp = m.total ? m.in_store * 100 / m.total : 0, running = t?.state === 'running';
      const store = running ? `${bar(t.bytes * 100 / Math.max(1, t.total))}<span class="sub">${fmt.size(t.bytes)} / ${fmt.size(t.total)} · ${fmt.bps(t.bps)}</span>` : t?.state === 'failed' ? `<span class="err">${esc(t.error)}</span>` : !m.total ? `<span class="mu">${esc(m.error || 'dosya listesi yok')}</span>` : sp >= 100 ? `${badge('hazır', 'ok')}` : `<span class="mu">${fmt.n(sp)}%</span>`;
      return `<tr><td><b>${esc(m.name)}</b><span class="sub">${esc(KINDS[m.kind] || m.kind)} · ${fmt.size(m.total)}${m.files > 1 ? ' · ' + m.files + ' dosya' : ''}</span></td><td style="min-width:150px">${store}</td>` +
        ns.map(n => { const c = m.nodes[n.id] || {}; const inner = c.complete ? `<span class="cl ok" title="hazır"></span>` : c.error ? `<span class="cl err" title="${esc(c.error)}"></span>` : c.active || c.queued ? `<div class="cb2" title="${c.active ? fmt.bps(c.bps) : 'sırada'}">${bar(c.pct)}</div>` : c.pct > 0 ? `<span class="mu">${fmt.n(c.pct)}%</span>` : `<button class="cl add" data-act="cell" data-id="${m.id}" data-n="${n.id}" title="Bu makineye aktar" ${n.status === 'online' && sp >= 100 ? '' : 'disabled'}></button>`; return `<td class="c">${inner}</td>`; }).join('') +
        `<td class="act">${!m.total ? `<button class="btn sm" data-act="refresh" data-id="${m.id}">Dosyaları al</button>` : sp < 100 ? `<button class="btn sm pri" data-act="dl" data-id="${m.id}" ${running ? 'disabled' : ''}>İndir</button>` : `<button class="btn sm" data-act="push" data-id="${m.id}">Hepsine dağıt</button>`}</td></tr>`;
    }).join('') || `<tr><td colspan="${ns.length + 3}">${emptyBox('Ağdan dağıtılan model yok', 'Modeller sayfasından HuggingFace ile ekleyin.')}</td></tr>`) + '</tbody></table>';
    const rows = [...d.transfers.map(x => ({ k: x.state, a: x.node, b: x.name, c: x.src ? x.src + ' → ' + x.node : x.state === 'queued' ? 'sırada' : x.state === 'failed' ? x.error : '', p: x.size ? x.bytes * 100 / x.size : 0, r: x.state === 'active' ? `${fmt.size(x.bytes)} / ${fmt.size(x.size)} · ${fmt.bps(x.bps)}` : fmt.size(x.size) })),
      ...d.ops.map(o => ({ k: 'active', a: o.node, b: o.label, c: o.step || '', p: null, r: fmt.dur(Date.now() / 1000 - o.t0) }))];
    $('#d-tr').innerHTML = rows.length ? rows.map(x => `<div class="tr">${sq(x.k === 'active' ? 'info' : x.k === 'failed' ? 'err' : '')}<b>${esc(x.a)}</b><span class="ell grow">${esc(x.b)} <span class="mu">${esc(x.c)}</span></span>${x.p != null ? `<div style="width:170px">${bar(x.p)}</div>` : ''}<span class="mu nw" style="min-width:190px;text-align:right">${x.r}</span></div>`).join('') : emptyBox('Süren iş yok');
    $('#d-nodes').innerHTML = `<table class="tbl"><thead><tr><th>Makine</th><th>Platform</th><th>Hızlandırıcı</th><th class="num">Boş disk</th><th class="num">Model</th><th>Runtime</th><th>Ajan</th><th></th></tr></thead><tbody>` + ns.map(n => {
      const u = n.total_gb ? (n.total_gb - n.free_gb) * 100 / n.total_gb : 0;
      return `<tr><td><b>${esc(n.name)}</b></td><td class="mono">${esc(n.platform || '—')}</td><td>${chips(n.backends) || '—'}</td><td class="num">${n.free_gb != null ? fmt.n(n.free_gb, 0) + ' GB' : '—'}<div style="margin-top:3px">${bar(u, loadCls(u))}</div></td><td class="num">${n.models.length}</td><td class="mono mu ell" style="max-width:240px" title="${esc(n.runtimes.join(', '))}">${n.runtimes.length ? esc(n.runtimes.join(', ')) : '—'}</td><td>${n.status !== 'online' ? '<span class="mu">kapalı</span>' : n.agent_ok ? badge(n.agent || 'güncel', 'ok') : badge('eski sürüm', 'warn')}</td><td class="act">${n.status === 'online' && !n.agent_ok ? `<button class="btn sm" data-act="agent" data-id="${n.id}">Güncelle</button>` : ''}</td></tr>`;
    }).join('') + '</tbody></table>';
  },
});
