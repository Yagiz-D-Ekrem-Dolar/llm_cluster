// Röle sunucusu (Node.js barındırma için). Bağımlılık yoktur.
// Siteniz buraya istek atar; küme dışarıdan içeri port açmadan bu röleye bağlanır (modem CGNAT arkasında olsa da).
//
//   RELAY_SECRET=GİZLİ PORT=3000 node relay.mjs
//
// Dışa açık uçlar : /v1/*  (site → küme), /healthz
// Küme uçları     : /__tunnel/poll, /__tunnel/push, /__tunnel/body  (Bearer GİZLİ)
import http from 'node:http';
import crypto from 'node:crypto';
import zlib from 'node:zlib';

const SECRET = process.env.RELAY_SECRET || '__RELAY_SECRET__';
const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.RELAY_HOST || '0.0.0.0';
const MAX_BODY = 32 * 1024 * 1024, INLINE_BODY = 64 * 1024, HEAD_WAIT = 330e3, ALIVE = 40e3, REDELIVER = 6e3;
const HOP = new Set(['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length']);
if (SECRET.length < 16 || SECRET.startsWith('__')) { console.error('Tünel anahtarı gerekli: RELAY_SECRET (panelden alın)'); process.exit(1); }

const items = new Map();
let queue = [], cancels = new Set(), waiters = [], lastPoll = 0;
const alive = () => Date.now() - lastPoll < ALIVE;
const wake = () => { const w = waiters; waiters = []; w.forEach(f => f()); };
const json = (res, code, obj) => { const b = Buffer.from(JSON.stringify(obj)); res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': b.length }); res.end(b); };
const err = (res, code, message, kind) => json(res, code, { error: { message, type: 'api_error', code: kind } });
const same = (a, b) => { const x = Buffer.from(a), y = Buffer.from(b); return x.length === y.length && crypto.timingSafeEqual(x, y); };

function readBody(req) {
  return new Promise((ok, no) => {
    const parts = []; let n = 0;
    req.on('data', c => { n += c.length; if (n > MAX_BODY) { no(Object.assign(new Error('büyük'), { big: true })); req.destroy(); } else parts.push(c); });
    req.on('end', () => ok(Buffer.concat(parts)));
    req.on('error', no);
  });
}
const describe = p => ({ id: p.id, m: p.method, p: p.path, q: p.query, h: p.headers, ip: p.ip, n: p.body.length, b: p.body.length <= INLINE_BODY ? p.body.toString('base64') : null });

function collect(limit) {
  const t = Date.now(), out = [];
  while (queue.length && out.length < limit) { const p = queue.shift(); p.delivered = t; out.push(p); }
  for (const p of items.values()) {
    if (out.length >= limit) break;
    if (p.delivered && !p.acked && t - p.delivered > REDELIVER && !out.includes(p)) { p.delivered = t; out.push(p); }
  }
  return out;
}

function deliver(p, f) { p.frames.push(f); const w = p.notify; p.notify = null; if (w) w(); }

async function onPoll(body, ip) {
  lastPoll = Date.now();
  const active = new Set(body.active || []);
  for (const id of body.ack || []) { const p = items.get(id); if (p && !p.acked) { p.acked = Date.now(); } active.add(id); }
  const t = Date.now();
  for (const p of items.values()) if (p.acked && p.acked < t - 3000 && !active.has(p.id)) deliver(p, { t: 'x', m: 'küme isteği kaybetti' });
  const deadline = t + Math.min(25e3, Number(body.wait ?? 20) * 1000), limit = Math.min(64, Number(body.max ?? 16));
  let got;
  for (;;) {
    got = collect(limit);
    if (got.length || cancels.size || Date.now() >= deadline) break;
    await new Promise(r => { const tm = setTimeout(r, Math.min(deadline - Date.now(), 2000)); waiters.push(() => { clearTimeout(tm); r(); }); });
    lastPoll = Date.now();
  }
  const c = [...cancels]; cancels.clear(); lastPoll = Date.now();
  return { v: 1, requests: got.map(describe), cancel: c, t: Date.now() / 1000, ip };
}

function onPush(raw) {
  let b = raw.subarray(1);
  if (raw[0] === 1) b = zlib.inflateSync(b);
  let off = 0;
  while (off < b.length) {
    const hl = b.readUInt32BE(off); off += 4;
    const h = JSON.parse(b.toString('utf8', off, off + hl)); off += hl;
    const dl = b.readUInt32BE(off); off += 4;
    h.d = b.subarray(off, off + dl); off += dl;
    const p = items.get(h.i);
    if (!p || h.s <= p.lastSeq) continue;
    p.lastSeq = h.s; deliver(p, h);
  }
  const c = [...cancels]; cancels.clear();
  return { ok: true, cancel: c };
}

async function onPublic(req, res, path, query) {
  if (req.method !== 'OPTIONS' && !(req.headers.authorization || req.headers['x-api-key'])) return err(res, 401, 'Kimlik doğrulama gerekli (Authorization: Bearer <anahtar>)', 'missing_api_key');
  let body;
  try { body = await readBody(req); } catch (e) { return e.big ? err(res, 413, 'İstek çok büyük', 'too_large') : undefined; }
  if (!alive()) return err(res, 503, 'Küme şu anda bağlı değil', 'cluster_offline');
  const headers = [];
  for (let i = 0; i < req.rawHeaders.length; i += 2) if (!HOP.has(req.rawHeaders[i].toLowerCase())) headers.push([req.rawHeaders[i], req.rawHeaders[i + 1]]);
  const ip = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim() || req.socket.remoteAddress;
  const p = { id: crypto.randomBytes(12).toString('hex'), method: req.method, path, query, headers, body, ip, frames: [], notify: null, delivered: 0, acked: 0, lastSeq: -1 };
  items.set(p.id, p); queue.push(p); wake();
  let started = false, closed = false;
  const onClose = () => { if (!res.writableEnded) { closed = true; cancels.add(p.id); wake(); const w = p.notify; p.notify = null; if (w) w(); } };
  res.on('close', onClose);
  req.socket.setNoDelay(true);
  const deadline = Date.now() + HEAD_WAIT;
  try {
    for (;;) {
      if (closed) return;
      if (!p.frames.length) {
        const wait = started ? 900e3 : Math.max(50, deadline - Date.now());
        const ok = await new Promise(r => { const tm = setTimeout(() => r(false), wait); p.notify = () => { clearTimeout(tm); r(true); }; });
        if (!ok) { if (!started) err(res, 504, 'Küme zamanında yanıt vermedi', 'cluster_timeout'); else res.destroy(); return; }
        continue;
      }
      const f = p.frames.shift();
      if (f.t === 'h') {
        started = true;
        const hd = {};
        for (const [k, v] of f.hd || []) if (!HOP.has(k.toLowerCase()) || k.toLowerCase() === 'content-length') hd[k] = v;
        hd['X-Accel-Buffering'] = 'no'; hd['Cache-Control'] = hd['Cache-Control'] || 'no-cache';
        res.writeHead(f.st, hd); res.flushHeaders();
      } else if (f.t === 'd') {
        if (f.d.length && !res.write(Buffer.from(f.d))) await new Promise(r => { res.once('drain', r); res.once('close', r); });
      } else if (f.t === 'e') { res.end(); return; }
      else if (f.t === 'x') { if (!started) err(res, 502, f.m || 'Küme hatası', 'cluster_error'); else res.destroy(); return; }
    }
  } finally { res.off('close', onClose); items.delete(p.id); queue = queue.filter(x => x !== p); if (!closed) cancels.delete(p.id); }
}

const server = http.createServer(async (req, res) => {
  try {
    const u = new URL(req.url, 'http://x'), path = u.pathname;
    let i = path.indexOf('/__tunnel/');
    if (i >= 0) {
      const a = path.slice(i + 10), key = (req.headers.authorization || '').slice(7) || req.headers['x-tunnel-key'] || '';
      if (!same(key, SECRET)) { req.resume(); return err(res, 401, 'Geçersiz tünel anahtarı', 'bad_tunnel_key'); }
      if (a === 'poll' && req.method === 'POST') {
        const ip = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim() || req.socket.remoteAddress;
        return json(res, 200, await onPoll(JSON.parse((await readBody(req)).toString() || '{}'), ip));
      }
      if (a === 'push' && req.method === 'POST') return json(res, 200, onPush(await readBody(req)));
      if (a === 'body' && req.method === 'GET') {
        const p = items.get(u.searchParams.get('id'));
        if (!p) return err(res, 404, 'İstek yok', 'not_found');
        res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': p.body.length }); return res.end(p.body);
      }
      return err(res, 404, 'Bulunamadı', 'not_found');
    }
    if (path.endsWith('/healthz')) return json(res, 200, { status: 'ok', cluster: alive(), age_s: lastPoll ? (Date.now() - lastPoll) / 1000 : null });
    i = path.indexOf('/v1/');
    if (i < 0) return err(res, 404, 'Bulunamadı', 'not_found');
    await onPublic(req, res, path.slice(i), u.search.slice(1));
  } catch (e) { console.error(e); if (!res.headersSent) err(res, 500, 'Röle hatası', 'relay_error'); else res.destroy(); }
});
server.requestTimeout = 0; server.headersTimeout = 60e3; server.keepAliveTimeout = 75e3;
server.listen(PORT, HOST, () => console.log(`röle dinliyor: ${HOST}:${PORT}`));
