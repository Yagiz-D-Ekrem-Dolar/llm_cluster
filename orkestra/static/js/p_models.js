'use strict';
/* Modeller: katalog, dağıtımlar, runtime'lar */

const KINDS = { llm: 'Sohbet', stt: 'Konuşma → metin', tts: 'Metin → konuşma', embedding: 'Gömme' };
const depSq = d => sq(d.desired === 'stopped' ? '' : d.state === 'ready' ? 'ok' : d.state === 'failed' ? 'err' : 'warn');
const MODEL_DEFAULTS = { name: '', repo: '', source: 'hf', file_glob: '', runtime: 'llamacpp', kind: 'llm', upstream: '', ctx: 8192, vram_gb: 0, ram_gb: 0, gpu_count: 1, cpu_ok: false,
  min_replicas: 0, max_replicas: 1, priority: 5, max_concurrency: 4, idle_unload_min: 15, startup_timeout_s: 900, prefer_tags: [], exclude_tags: [], extra_args: '', enabled: true, notes: '' };

function modelDialog(m, rts, prefill = {}) {
  const v = m || { ...MODEL_DEFAULTS, ...prefill };
  const num = (name, label, val, extra = '', hint = '') => fld(label, `<input class="inp" type="number" name="${name}" value="${val ?? ''}" ${extra}>`, hint);
  const dlg = modal({ title: m ? `${esc(m.name)} · düzenle` : 'Model ekle', size: 'wide', body: `<form id="mf"><div class="form-g">
    ${fld('Ad', `<input class="inp" name="name" value="${esc(v.name)}" placeholder="qwen2.5-7b-instruct">`, 'API\'de model adı olarak kullanılır')}
    ${fld('HuggingFace yolu', `<input class="inp mono" name="repo" value="${esc(v.repo)}" placeholder="Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M">`, 'kullanici/depo, GGUF için :NICEMLEME')}
    ${fld('Dosyalar makinelere nasıl gitsin', `<select class="inp" name="source">${[['hf', 'Ana PC indirir, switch üzerinden dağıtır'], ['runtime', 'Runtime kendisi halleder']].map(([k, l]) => opt(k, l, v.source)).join('')}</select>`)}
    ${fld('Dosya deseni', `<input class="inp mono" name="file_glob" value="${esc(v.file_glob || '')}" placeholder="boş: otomatik seçim">`, 'Ör. ggml-large-v3-turbo.bin ya da tr/tr_TR/*')}
    ${fld('Runtime', `<select class="inp" name="runtime">${rts.map(r => opt(r.key, r.label, v.runtime)).join('')}</select>`)}
    ${fld('Tür', `<select class="inp" name="kind">${Object.entries(KINDS).map(([k, l]) => opt(k, l, v.kind)).join('')}</select>`)}
    ${num('ctx', 'Bağlam uzunluğu', v.ctx)}${num('gpu_count', 'GPU sayısı', v.gpu_count, 'min="0"', '0 = yalnızca CPU')}
    ${fld('Toplam VRAM (GB)', `<div class="row"><input class="inp" type="number" step="0.1" name="vram_gb" value="${v.vram_gb || ''}" placeholder="otomatik"><button type="button" class="btn" id="mf-est">Tahmin et</button></div>`, 'Boşsa model adından tahmin edilir')}
    ${num('ram_gb', 'RAM (GB)', v.ram_gb || '', 'step="0.1" placeholder="otomatik"')}
    ${fld('Replika (en az – en çok)', `<div class="row"><input class="inp" type="number" name="min_replicas" min="0" value="${v.min_replicas}"><input class="inp" type="number" name="max_replicas" min="0" value="${v.max_replicas}"></div>`, 'En az 1 ise hep açık kalır')}
    ${num('priority', 'Öncelik (1–10)', v.priority, 'min="1" max="10"', 'Yer gerekince düşük öncelikli boştaki model kaldırılır')}
    ${num('max_concurrency', 'Replika başına eşzamanlı istek', v.max_concurrency, 'min="1"')}
    ${num('idle_unload_min', 'Boşta kaldırma (dk)', v.idle_unload_min, 'min="0"')}${num('startup_timeout_s', 'Başlatma zaman aşımı (sn)', v.startup_timeout_s, 'min="30"', 'İlk indirme uzun sürebilir')}
    ${fld('Tercih edilen etiketler', `<input class="inp" name="prefer_tags" value="${esc((v.prefer_tags || []).join(', '))}" placeholder="main, gpu">`)}
    ${fld('Kaçınılan etiketler', `<input class="inp" name="exclude_tags" value="${esc((v.exclude_tags || []).join(', '))}" placeholder="laptop">`)}
    <div class="full">${fld('Ek argümanlar', `<input class="inp mono" name="extra_args" value="${esc(v.extra_args)}">`, 'Runtime komutunun sonuna eklenir')}</div>
    <div class="full">${fld('Not', `<input class="inp" name="notes" value="${esc(v.notes)}">`)}</div>
    <div class="full col">${chk('cpu_ok', 'GPU\'ya sığmazsa CPU\'da çalışabilir', v.cpu_ok)}${chk('enabled', 'Etkin', v.enabled)}</div></div></form>`,
    actions: [m && { label: 'Sil', kind: 'danger', icon: 'trash', close: false, onClick: async mm => {
        if (await confirmBox(`<b>${esc(m.name)}</b> ve tüm dağıtımları silinecek.`, { danger: true, ok: 'Sil' })) { await del(`/api/models/${m.id}`); toast('Model silindi', 'ok'); mm.close(); }
      } }, { label: 'Vazgeç' }, { label: m ? 'Kaydet' : 'Ekle', kind: 'pri', onClick: async mm => {
      const f = formVals($('#mf', mm.el));
      const body = { ...f, file_glob: f.file_glob.trim(), vram_gb: f.vram_gb || 0, ram_gb: f.ram_gb || 0, ctx: f.ctx || 0, gpu_count: f.gpu_count ?? 1, min_replicas: f.min_replicas ?? 0, max_replicas: f.max_replicas ?? 1,
        priority: f.priority || 5, max_concurrency: f.max_concurrency || 4, idle_unload_min: f.idle_unload_min ?? 15, startup_timeout_s: f.startup_timeout_s || 900 };
      if (m) await patch(`/api/models/${m.id}`, body); else await post('/api/models', body);
      toast(m ? 'Kaydedildi' : 'Model eklendi', 'ok');
    } }].filter(Boolean) });
  $('#mf-est', dlg.el).onclick = async () => {
    const f = formVals($('#mf', dlg.el));
    try { const r = await api(`/api/estimate?repo=${encodeURIComponent(f.repo)}&extra=${encodeURIComponent(f.extra_args)}`); $('[name=vram_gb]', dlg.el).value = r.size_gb; } catch (e) { toastErr(e); }
  };
}

function hfDialog(rts) {
  const KT = [['llm', 'Sohbet'], ['stt', 'Konuşma → metin'], ['tts', 'Metin → konuşma'], ['embedding', 'Gömme']];
  let kind = 'llm';
  const SAMPLE = { llm: 'qwen2.5, llama 3.1, gemma…', stt: 'whisper, faster-whisper…', tts: 'piper, kokoro…', embedding: 'bge-m3…' };
  const SUGGEST = { stt: [['ggerganov/whisper.cpp', 'whisper.cpp model dosyaları']], tts: [['rhasspy/piper-voices', 'Piper sesleri, Türkçe dahil']] };
  const slug = s => s.toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);
  const cnt = n => n >= 1e6 ? (n / 1e6).toFixed(1).replace('.', ',') + ' Mn' : n >= 1e3 ? Math.round(n / 1e3) + ' B' : String(n || 0);
  const dlg = modal({ title: 'HuggingFace\'ten model ekle', size: 'xl', body: `
    <div class="row wrap mb"><div class="seg" id="hf-k">${KT.map(([k, l]) => `<button type="button" data-k="${k}" class="${k === kind ? 'on' : ''}">${l}</button>`).join('')}</div>
      <input class="inp" id="hf-q" style="max-width:300px"><label class="chk"><input type="checkbox" id="hf-gguf"><span>yalnızca GGUF</span></label><button class="btn pri" id="hf-go">${icon('search')}Ara</button></div>
    <div class="hf-cols"><div class="card hf-box" id="hf-res"><div class="empty">Aranıyor…</div></div><div class="card hf-box" id="hf-det"><div class="empty">Soldan bir model seçin</div></div></div>
    <div class="mu mt" style="font-size:12px">Seçilen dosyalar ana PC'ye bir kez iner, makinelere switch üzerinden gider.</div>`, actions: [{ label: 'Kapat' }] });
  const setKind = () => { $('#hf-q', dlg.el).placeholder = 'Ara: ' + SAMPLE[kind]; $('#hf-gguf', dlg.el).closest('label').classList.toggle('hide', kind !== 'llm'); };
  const search = async () => {
    const res = $('#hf-res', dlg.el); res.innerHTML = '<div class="empty">Aranıyor…</div>';
    try {
      const rows = await api(`/api/hf/search?q=${encodeURIComponent($('#hf-q', dlg.el).value)}&kind=${kind}&gguf=${kind === 'llm' && $('#hf-gguf', dlg.el).checked ? 1 : 0}`);
      const q = $('#hf-q', dlg.el).value.trim(), top = /^[\w.-]+\/[\w.-]+$/.test(q) ? [[q, 'doğrudan aç']] : q ? [] : (SUGGEST[kind] || []);
      res.innerHTML = top.map(([id, t]) => `<div class="hf-r" data-id="${esc(id)}"><b>${esc(id)}</b><div class="mu" style="font-size:11.5px">${esc(t)}</div></div>`).join('') + rows.filter(x => !top.some(t => t[0] === x.id)).map(x => `<div class="hf-r" data-id="${esc(x.id)}"><b>${esc(x.id)}</b><div class="row wrap mu" style="gap:8px;font-size:11.5px"><span>${cnt(x.downloads)} indirme</span><span>${x.likes} beğeni</span>${x.gguf ? '<span class="chip pri">GGUF</span>' : ''}${x.gated ? '<span class="chip warn">kısıtlı</span>' : ''}</div></div>`).join('') || emptyBox('Sonuç yok');
      const first = $('.hf-r', res); if (top.length && first) open(first.dataset.id);
    } catch (e) { res.innerHTML = `<div class="empty err">${esc(e.message)}</div>`; }
  };
  const open = async id => {
    $$('.hf-r', dlg.el).forEach(r => r.classList.toggle('on', r.dataset.id === id));
    const det = $('#hf-det', dlg.el); det.innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const repo = await api('/api/hf/repo?id=' + encodeURIComponent(id)), V = repo.variants || [];
      const rtFor = t => ({ gguf: 'llamacpp', ggml: 'whispercpp', voice: 'piper' })[t] || (kind === 'llm' ? 'vllm' : kind === 'embedding' ? 'vllm' : 'speaches');
      let cur = V.find(x => x.tag === 'Q4_K_M') || V.find(x => /large-v3-turbo-q5/.test(x.id)) || V[Math.floor((V.length - 1) / 2)] || V[0];
      if (V[0]?.type === 'voice') cur = V.find(x => /tr_TR/.test(x.id)) || V[0];
      det.innerHTML = `<div class="card-b col"><div><b>${esc(repo.id)}</b> <span class="mu">${repo.params_b ? repo.params_b + ' Mr parametre · ' : ''}${esc(repo.license || '')}</span></div>
        ${repo.gated ? `<div class="alert" style="margin:0">${icon('alert')}<div>Kısıtlı depo: HuggingFace'te erişim isteyin ve HuggingFace sayfasına token girin.</div></div>` : ''}
        ${V.length > 8 ? '<input class="inp" id="hf-f" placeholder="Listede ara (ör. tr_TR, q5)">' : ''}<div class="card" id="hf-v" style="max-height:210px;overflow:auto"></div>
        <div class="form-g">${fld('Runtime', `<select class="inp" id="hf-rt"></select>`)}${fld('Model adı', '<input class="inp mono" id="hf-name">')}</div><button class="btn pri" id="hf-add">${icon('plus')}Kataloğa ekle…</button></div>`;
      const nameFor = x => slug(repo.id.split('/')[1].replace(/-GGUF$/i, '') + (x.type === 'gguf' ? '-' + x.tag : x.type === 'repo' ? '' : '-' + x.label.split('/').pop().replace(/\.(bin|onnx)$/, '').replace(/^ggml-/, '')));
      const pick = x => { cur = x; $('#hf-rt', det).innerHTML = rts.map(r => opt(r.key, r.label, rtFor(x.type))).join(''); $('#hf-name', det).value = nameFor(x); draw(); };
      const draw = () => { const q = ($('#hf-f', det)?.value || '').toLowerCase(), list = V.filter(x => x.id === cur.id || x.label.toLowerCase().includes(q)).slice(0, 120);
        $('#hf-v', det).innerHTML = `<table class="tbl"><tbody>${list.map(x => `<tr class="click ${x.id === cur.id ? 'sel' : ''}" data-v="${esc(x.id)}"><td class="mono ell" style="max-width:300px" title="${esc(x.label)}">${esc(x.label)}</td><td class="num">${fmt.size(x.size)}</td></tr>`).join('')}</tbody></table>`; };
      on($('#hf-v', det), 'click', '[data-v]', r => pick(V.find(x => x.id === r.dataset.v)));
      if ($('#hf-f', det)) $('#hf-f', det).oninput = draw;
      pick(cur);
      $('#hf-add', det).onclick = () => {
        const gb = cur.size / 1024 ** 3, cpu = cur.type === 'voice', gguf = cur.type === 'gguf';
        const prefill = { name: $('#hf-name', det).value, repo: gguf ? `${repo.id}:${cur.tag}` : repo.id, file_glob: cur.glob || '', source: 'hf', runtime: $('#hf-rt', det).value, kind,
          vram_gb: cpu ? 0 : +(gb * 1.1 + (kind === 'llm' ? 1 : 0.5)).toFixed(1), ram_gb: cpu ? 0.8 : 0, gpu_count: cpu ? 0 : 1, cpu_ok: cpu || gguf || cur.type === 'ggml', ctx: kind === 'llm' ? 8192 : 0, notes: 'HuggingFace: ' + repo.id };
        dlg.close(); modelDialog(null, rts, prefill);
      };
    } catch (e) { det.innerHTML = `<div class="empty err">${esc(e.message)}</div>`; }
  };
  on(dlg.el, 'click', '#hf-k button', b => { kind = b.dataset.k; $$('#hf-k button', dlg.el).forEach(x => x.classList.toggle('on', x === b)); setKind(); search(); });
  on(dlg.el, 'click', '.hf-r', r => open(r.dataset.id));
  $('#hf-go', dlg.el).onclick = search;
  $('#hf-q', dlg.el).onkeydown = e => { if (e.key === 'Enter') search(); };
  setKind(); search();
}

function buildDialog(rt, done) {
  let src = 'file', file = null;
  const dlg = modal({ title: `${esc(rt.label)} · derleme ekle`, size: 'wide', body: `<form id="bf">
    <div class="seg mb" id="b-s">${[['file', 'Dosya yükle'], ['path', 'Bu bilgisayardaki yol'], ['git', 'Git deposu']].map(([k, l]) => `<button type="button" data-s="${k}" class="${k === src ? 'on' : ''}">${l}</button>`).join('')}</div>
    <div id="b-x" class="mb"></div><div class="form-g">
      ${fld('Tür', `<select class="inp" name="format">${opt('bin', 'Hazır derleme (tar.gz / zip / tek dosya)', 'bin')}${opt('src', 'Kaynak kod: her makine kendisi derler', 'bin')}</select>`)}${fld('Sürüm etiketi', '<input class="inp" name="version" placeholder="yamali-2026-11">')}
      <div class="b-bin">${fld('Platform', `<select class="inp" name="platform">${['linux-x86_64', 'linux-aarch64', 'windows-x86_64', 'any'].map(x => opt(x, x === 'any' ? 'hepsi' : x, 'linux-x86_64')).join('')}</select>`)}</div>
      <div class="b-bin">${fld('Hızlandırıcı', `<select class="inp" name="backend">${[['cuda', 'NVIDIA CUDA'], ['vulkan', 'Vulkan (AMD/Intel)'], ['cpu', 'Yalnızca CPU'], ['any', 'Hepsi']].map(([k, l]) => opt(k, l, 'cuda')).join('')}</select>`, 'Her makineye donanımına uyan derleme gider')}</div>
      ${fld('Çalıştırılacak dosya', `<input class="inp mono" name="entry" placeholder="otomatik: ${esc(rt.exe)}">`, 'Paket içindeki göreli yol')}<div class="b-src hide">${fld('Ek CMake seçenekleri', '<input class="inp mono" name="cmake" placeholder="-DGGML_AVX512=ON">', 'Kaynak kökünde orkestra-build.sh varsa o çalışır')}</div></div>
    <div class="mt" id="b-p"></div></form>`,
    actions: [{ label: 'Vazgeç' }, { label: 'Ekle ve dağıt', kind: 'pri', onClick: async mm => {
      const f = formVals($('#bf', mm.el)), body = { runtime: rt.key, format: f.format, version: f.version, platform: f.platform, backend: f.backend, entry: f.entry, cmake: f.cmake };
      if (src === 'file') {
        if (!file) throw new Error('Dosya seçin');
        const up = await new Promise((ok, no) => { const x = new XMLHttpRequest(); x.open('PUT', '/api/fabric/upload?name=' + encodeURIComponent(file.name)); x.setRequestHeader('X-Orkestra', '1');
          x.upload.onprogress = e => { $('#b-p', mm.el).innerHTML = `${bar(e.loaded * 100 / e.total)}<div class="mu mt">${fmt.size(e.loaded)} / ${fmt.size(e.total)}</div>`; };
          x.onload = () => x.status < 300 ? ok(JSON.parse(x.responseText)) : no(new Error('Yükleme başarısız: ' + x.status)); x.onerror = () => no(new Error('Yükleme kesildi')); x.send(file); });
        body.sha256 = up.sha256; body.filename = up.name;
      } else if (src === 'path') body.path = f.path; else { body.git_url = f.git_url; body.git_ref = f.git_ref; }
      await post('/api/fabric/builds', body); toast('Derleme eklendi; gereken makinelere gidecek', 'ok'); done && done();
    } }] });
  const paint = () => { $('#b-x', dlg.el).innerHTML = src === 'file' ? `<input class="inp" type="file" id="b-file" style="padding:3px">` : src === 'path' ? fld('Dosya ya da klasör', `<input class="inp mono" name="path" placeholder="C:\\\\llama.cpp\\\\build\\\\bin">`, 'Bu bilgisayardaki yol. Klasör paketlenir.') : `<div class="form-g">${fld('Depo adresi', '<input class="inp mono" name="git_url" placeholder="https://github.com/kurum/llama.cpp">')}${fld('Dal / etiket', '<input class="inp mono" name="git_ref" placeholder="yamali">')}</div>`; if (src === 'file') $('#b-file', dlg.el).onchange = e => { file = e.target.files[0]; }; if (src === 'git') { $('[name=format]', dlg.el).value = 'src'; mode(); } };
  const mode = () => { const s = $('[name=format]', dlg.el).value === 'src'; $$('.b-bin', dlg.el).forEach(x => x.classList.toggle('hide', s)); $$('.b-src', dlg.el).forEach(x => x.classList.toggle('hide', !s)); };
  on($('#b-s', dlg.el), 'click', '[data-s]', b => { src = b.dataset.s; $$('#b-s button', dlg.el).forEach(x => x.classList.toggle('on', x === b)); paint(); });
  $('[name=format]', dlg.el).onchange = mode; paint();
}

async function runtimeDialog(r, clone = false) {
  const nodes = await api('/api/nodes').catch(() => []);
  const isNew = !r || clone;
  const v = r || { key: '', label: '', exe: '', args: [], env: {}, health: '/health', needs_gpu: false, notes: '', paths: {} };
  const P = v.paths || {};
  const PK = [['chat', 'Sohbet'], ['completions', 'Tamamlama'], ['embeddings', 'Gömme'], ['stt', 'Konuşma → metin'], ['tts', 'Metin → konuşma']];
  const std = { chat: '/v1/chat/completions', completions: '/v1/completions', embeddings: '/v1/embeddings', stt: '/v1/audio/transcriptions', tts: '/v1/audio/speech' };
  const avail = !isNew && nodes.length ? `<div class="sec">Sunucularda kullanılabilirlik</div><div class="card"><div class="tbl-w"><table class="tbl"><thead><tr><th>Sunucu</th><th>Çalıştırılabilir dosya</th><th>Durum</th><th>Çözüm</th></tr></thead><tbody>` + nodes.map(n => {
    const exe = (n.rt_paths || {})[v.key] || v.exe, known = n.allow_exe != null && !n.sim, ok = !known || (n.runtime_paths || {})[exe] != null;
    return `<tr><td><b>${esc(n.name)}</b></td><td class="mono ell" style="max-width:280px" title="${esc(exe)}">${esc(exe)}</td><td>${known ? (ok ? badge('bulundu', 'ok') : badge('yok ya da izinli değil', 'err')) : '<span class="mu">bilinmiyor</span>'}</td><td class="mono mu" style="font-size:11.5px">${known && !ok ? 'sudo orkestra-agent-allow ' + esc(exe) : ''}</td></tr>`;
  }).join('') + '</tbody></table></div></div>' : '';
  modal({ title: isNew ? 'Özel runtime' : `${esc(r.label)} · düzenle`, size: 'wide', body: `<form id="rf"><div class="form-g">
    ${isNew ? fld('Anahtar', '<input class="inp mono" name="key" placeholder="llamacpp-yamali">', 'küçük harf, rakam, - ve _') : ''}${fld('Görünen ad', `<input class="inp" name="label" value="${esc(v.label)}">`)}
    ${fld('Çalıştırılabilir dosya', `<input class="inp mono" name="exe" value="${esc(v.exe)}" placeholder="llama-server ya da /opt/llama-yamali/build/bin/llama-server">`, 'Ad ya da tam yol. Makineye özel yol: Sunucular → Ayarlar. Ajanda izinli olmalıdır.')}${fld('Sağlık uç noktası', `<input class="inp mono" name="health" value="${esc(v.health)}">`)}
    <div class="full">${fld('Argümanlar (satır başına bir tane)', `<textarea class="inp mono" name="args" rows="7">${esc((v.args || []).join('\n'))}</textarea>`, '{repo} {name} {port} {ctx} {ngl} {parallel} {gpu_util} değişkenleri kullanılabilir')}</div>
    <div class="full">${fld('Ortam değişkenleri (KEY=DEĞER satırları)', `<textarea class="inp mono" name="env" rows="3" placeholder="LD_LIBRARY_PATH=/opt/llama-yamali/build/bin">${esc(Object.entries(v.env || {}).map(([k, x]) => k + '=' + x).join('\n'))}</textarea>`)}</div>
    <div class="full"><details ${Object.keys(P).length ? 'open' : ''}><summary class="mu" style="cursor:pointer;font-weight:600">API uç nokta yolları (yamalı sürümde standarttan farklıysa)</summary><div class="form-g mt">${PK.map(([k, l]) => fld(l, `<input class="inp mono" name="path_${k}" value="${esc(P[k] || '')}" placeholder="${std[k]}">`)).join('')}</div></details></div>
    <div class="full">${fld('Not', `<textarea class="inp" name="notes" rows="2">${esc(v.notes)}</textarea>`)}</div>
    <div class="full">${chk('needs_gpu', 'NVIDIA GPU gerektirir (GPU\'suz sunucularda kullanılmaz)', v.needs_gpu)}</div></div></form>${avail}`,
    actions: [!isNew && !r.builtin && { label: 'Sil', kind: 'danger', icon: 'trash', close: false, onClick: async mm => { if (await confirmBox('Bu runtime silinecek.', { danger: true, ok: 'Sil' })) { await del(`/api/runtimes/${r.key}`); mm.close(); } } },
      !isNew && { label: 'Çoğalt', icon: 'copy', close: false, onClick: mm => { mm.close(); runtimeDialog({ ...r, key: '', label: r.label + ' (kopya)', builtin: false }, true); } },
      { label: 'Vazgeç' }, { label: 'Kaydet', kind: 'pri', onClick: async mm => {
        const f = formVals($('#rf', mm.el)), paths = {};
        PK.forEach(([k]) => { if (f['path_' + k]) paths[k] = f['path_' + k]; });
        const body = { key: f.key, label: f.label, exe: f.exe, health: f.health, env: f.env, notes: f.notes, needs_gpu: f.needs_gpu, paths, args: f.args.split('\n').map(x => x.trim()).filter(Boolean) };
        if (isNew) await post('/api/runtimes', body); else await put(`/api/runtimes/${r.key}`, body);
        toast('Kaydedildi', 'ok');
      } }].filter(Boolean) });
}

App.register('models', {
  title: 'Modeller', tab: 'cat', models: [], deps: [], rts: [], fab: null,
  mount(el) {
    this.models = []; this.deps = [];
    el.innerHTML = `<div class="cmd"><button class="cb" id="m-hf">${icon('search')}HuggingFace'ten ekle</button><button class="cb" id="m-add">${icon('plus')}Elle ekle</button><span class="sep"></span><button class="cb" id="m-rt">${icon('plus')}Yeni runtime</button><span class="grow"></span><span class="mu" id="m-sum"></span></div>
      <div class="tabs">${[['cat', 'Model kataloğu'], ['dep', 'Dağıtımlar'], ['rt', 'Runtime\'lar']].map(([k, l]) => `<button class="tab ${k === this.tab ? 'on' : ''}" data-t="${k}">${l}</button>`).join('')}</div><div id="m-body"></div>`;
    $('#m-add').onclick = () => modelDialog(null, this.rts);
    $('#m-hf').onclick = () => hfDialog(this.rts);
    $('#m-rt').onclick = () => runtimeDialog(null);
    on(el, 'click', '.tab', b => { this.tab = b.dataset.t; $$('.tab', el).forEach(x => x.classList.toggle('on', x === b)); this.render(); });
    on(el, 'click', '[data-m]', async b => {
      const id = +b.dataset.id, act = b.dataset.m;
      try {
        if (act === 'edit') modelDialog(this.models.find(x => x.id === id), this.rts);
        else if (act === 'deploy') { const d = await post(`/api/models/${id}/deploy`, {}); toast(`Dağıtım başlatıldı: ${d.node}:${d.port}`, 'ok'); }
        else if (act === 'rt') runtimeDialog(this.rts.find(x => x.key === b.dataset.k));
        else if (act === 'build') buildDialog(this.rts.find(x => x.key === b.dataset.k), () => this.load());
        else if (act === 'rollout') { const r = await post('/api/fabric/rollout', { runtime: b.dataset.k }); toast(r.count ? `${r.count} replika sırayla güncelleniyor` : 'Tüm replikalar güncel', 'ok'); }
        else if (['activate', 'deactivate', 'delete'].includes(act)) { if (act !== 'delete' || await confirmBox('Bu derleme silinecek.', { danger: true, ok: 'Sil' })) await post(`/api/fabric/builds/${b.dataset.b}/action`, { action: act }); }
        else if (act === 'log') openLogs(+b.dataset.n, `dep-${id}`);
        else if (act === 'remove') { if (await confirmBox('Bu dağıtım kaldırılacak.', { danger: true, ok: 'Kaldır' })) await post(`/api/deployments/${id}/action`, { action: 'remove' }); }
        else await post(`/api/deployments/${id}/action`, { action: act });
        this.load();
      } catch (e) { toastErr(e); }
    });
    App.every(() => this.load(), 3000);
  },
  async load() {
    [this.models, this.deps, this.rts] = await Promise.all([api('/api/models'), api('/api/deployments'), api('/api/runtimes')]);
    if (this.tab === 'rt') this.fab = await api('/api/fabric');
    this.render();
  },
  render() {
    const body = $('#m-body'); if (!body) return;
    const ready = this.deps.filter(d => d.state === 'ready').length;
    $('#m-sum').textContent = `${this.models.length} model · ${ready}/${this.deps.length} dağıtım hazır`;
    if (this.tab === 'cat') {
      body.innerHTML = `<div class="card"><div class="tbl-w"><table class="tbl"><thead><tr><th>Model</th><th>Runtime</th><th>HuggingFace yolu</th><th>Bellek</th><th>Replika</th><th class="num">Talep</th><th></th></tr></thead><tbody>` +
        (this.models.map(m => `<tr><td><b>${esc(m.name)}</b><span class="sub">${esc(KINDS[m.kind] || m.kind)}${m.enabled ? '' : ' · devre dışı'} · öncelik ${m.priority}</span></td>
          <td>${esc(m.runtime_label)}</td><td class="mono ell" style="max-width:240px" title="${esc(m.repo)}">${esc(m.repo)}${m.files_gb ? `<span class="sub">${fmt.n(m.files_gb, 1)} GB dosya</span>` : ''}</td>
          <td>${m.gpu_count ? fmt.n(m.size_gb, 1) + ' GB' + (m.gpu_count > 1 ? ` · ${m.gpu_count} GPU` : '') : 'CPU'}${m.cpu_ok && m.gpu_count ? '<span class="sub">CPU yedeği</span>' : ''}</td>
          <td>${m.deployments.map(d => `<span title="${esc(d.node)}:${d.port} · ${esc(depLabel(d))}">${depSq(d)}</span>`).join(' ') || '<span class="mu">—</span>'}<span class="sub">${m.ready} hazır · ${m.min_replicas}–${m.max_replicas}</span></td>
          <td class="num">${m.demand.inflight}${m.demand.waiting ? ` <span class="warn">+${m.demand.waiting}</span>` : ''}<span class="sub">${m.demand.rpm} istek/dk</span></td>
          <td class="act"><button class="btn sm" data-m="deploy" data-id="${m.id}">Dağıt</button> <button class="btn sm" data-m="edit" data-id="${m.id}">Düzenle</button></td></tr>`).join('')
          || `<tr><td colspan="7">${emptyBox('Model tanımlı değil', '“HuggingFace\'ten ekle” ile sohbet ve ses modellerini ekleyin.')}</td></tr>`) + '</tbody></table></div></div>';
    } else if (this.tab === 'dep') {
      body.innerHTML = `<div class="card"><div class="tbl-w"><table class="tbl"><thead><tr><th>Model</th><th>Sunucu</th><th>Durum</th><th>Kaynak</th><th class="num">İş</th><th class="num">tok/sn</th><th class="num">İlk token</th><th></th></tr></thead><tbody>` +
        (this.deps.map(d => `<tr><td><b>${esc(d.model)}</b>${d.error ? `<span class="sub err">${esc(d.error.slice(0, 100))}</span>` : ''}</td><td>${esc(d.node)}<span class="sub mono">:${d.port}</span></td><td>${depBadge(d)}</td>
          <td>${d.pinned ? '<span class="tag">sabit</span>' : '<span class="tag">otomatik</span>'}<span class="tag">${d.gpus.length ? 'GPU ' + d.gpus.join(',') : 'CPU'}</span></td>
          <td class="num">${d.inflight}/${d.max_conc}</td><td class="num">${d.tps || '—'}</td><td class="num">${d.ttft_ms ? fmt.ms(d.ttft_ms) : '—'}</td>
          <td class="act"><button class="btn sm" data-m="log" data-id="${d.id}" data-n="${d.node_id}">Günlük</button>
            <button class="btn sm" data-m="${d.desired === 'stopped' ? 'start' : 'stop'}" data-id="${d.id}">${d.desired === 'stopped' ? 'Başlat' : 'Durdur'}</button>
            <button class="btn sm" data-m="restart" data-id="${d.id}">Yeniden başlat</button>
            <button class="btn sm" data-m="${d.pinned ? 'unpin' : 'pin'}" data-id="${d.id}">${d.pinned ? 'Sabitlemeyi kaldır' : 'Sabitle'}</button>
            <button class="btn sm danger" data-m="remove" data-id="${d.id}">Kaldır</button></td></tr>`).join('')
          || `<tr><td colspan="8">${emptyBox('Dağıtım yok', 'Talep geldiğinde otomatik yüklenir ya da kataloğdan elle dağıtın.')}</td></tr>`) + '</tbody></table></div></div>';
    } else {
      const all = this.fab?.builds || [];
      body.innerHTML = this.rts.map(r => { const bs = all.filter(b => b.runtime === r.key);
        return `<div class="card mb"><div class="card-h"><h3>${esc(r.label)}</h3><span class="tag">${esc(r.key)}</span>${r.needs_gpu ? '<span class="tag">GPU gerekir</span>' : ''}
        <div class="sp"><button class="btn sm pri" data-m="build" data-k="${esc(r.key)}">${icon('upload')}Derleme ekle</button>${bs.length ? `<button class="btn sm" data-m="rollout" data-k="${esc(r.key)}">Çalışanları güncelle</button>` : ''}<button class="btn sm" data-m="rt" data-k="${esc(r.key)}">Komutu düzenle</button></div></div>
        ${bs.length ? `<div class="tbl-w"><table class="tbl"><thead><tr><th>Sürüm</th><th>Platform</th><th>Hızlandırıcı</th><th>Tür</th><th>Dosya</th><th class="num">Boyut</th><th>Durum</th><th></th></tr></thead><tbody>${bs.map(b => `<tr><td><b>${esc(b.version || b.id)}</b></td><td class="mono">${esc(b.platform === 'any' ? 'hepsi' : b.platform)}</td><td>${esc(b.backend === 'any' ? 'makineye göre' : b.backend)}</td><td>${b.format === 'src' ? 'kaynak (makinede derlenir)' : 'hazır'}</td><td class="mono ell" style="max-width:190px">${esc(b.filename)}</td><td class="num">${fmt.size(b.size)}</td><td>${b.active ? badge('etkin', 'ok') : badge('pasif', 'idle')}</td><td class="act"><button class="btn sm" data-m="${b.active ? 'deactivate' : 'activate'}" data-b="${esc(b.id)}">${b.active ? 'Pasifleştir' : 'Etkinleştir'}</button> <button class="btn sm danger" data-m="delete" data-b="${esc(b.id)}">Sil</button></td></tr>`).join('')}</tbody></table></div>` : ''}
        <div class="card-b"><code style="word-break:break-all">${esc([bs.length ? '<dağıtılan derleme>' : r.exe, ...r.args].join(' '))}</code><div class="mu" style="margin-top:6px">${esc(r.notes)}</div></div></div>`; }).join('');
    }
  },
});
