<?php
/*
 * Röle (PHP paylaşımlı barındırma için): bu tek dosyayı sitenizin altına yükleyin, ör. public_html/llm/relay.php
 *
 *   Site → https://siteniz.com/llm/relay.php/v1/chat/completions
 *   Küme → aynı adrese dışarıdan bağlanır (modem CGNAT arkasında olsa da çalışır)
 *
 * Veritabanı gerekmez; aynı klasörde gizli bir çalışma klasörü oluşturur.
 * Her açık sohbet akışı bir PHP işçisi kullanır: yoğun trafikte Node.js ya da VPS rölesini tercih edin.
 */
declare(strict_types=1);

const TUNNEL_SECRET = '__RELAY_SECRET__';
const MAX_BODY = 33554432;      // 32 MB
const INLINE_BODY = 65536;
const HEAD_WAIT = 330;          // kümenin yanıta başlaması için en çok (sn)
const ALIVE_S = 40;
const REDELIVER_S = 6;
const SPOOL = __DIR__ . '/.relay-' . 'spool';

@ini_set('display_errors', '0');
@ini_set('zlib.output_compression', '0');
@ini_set('output_buffering', '0');
@ini_set('implicit_flush', '1');
@set_time_limit(0);
ignore_user_abort(true);

function jout(int $code, array $o): void {
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($o, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
}
function fail(int $code, string $msg, string $kind): void { jout($code, ['error' => ['message' => $msg, 'type' => 'api_error', 'code' => $kind]]); }

function spool_ready(): void {
    foreach (['', '/q', '/d', '/b', '/r', '/c', '/s', '/m'] as $d) if (!is_dir(SPOOL . $d)) @mkdir(SPOOL . $d, 0700, true);
    if (!is_file(SPOOL . '/.htaccess')) @file_put_contents(SPOOL . '/.htaccess', "Require all denied\nDeny from all\n");
    if (!is_file(SPOOL . '/index.html')) @touch(SPOOL . '/index.html');
}
function req_headers(): array {
    $out = [];
    if (function_exists('getallheaders')) foreach (getallheaders() as $k => $v) $out[strtolower((string)$k)] = [(string)$k, (string)$v];
    foreach ($_SERVER as $k => $v) {
        if (strncmp($k, 'HTTP_', 5) === 0) { $n = strtolower(str_replace('_', '-', substr($k, 5))); $out[$n] ??= [str_replace(' ', '-', ucwords(str_replace('-', ' ', $n))), (string)$v]; }
    }
    foreach (['CONTENT_TYPE' => 'Content-Type', 'CONTENT_LENGTH' => 'Content-Length', 'REDIRECT_HTTP_AUTHORIZATION' => 'Authorization'] as $k => $n)
        if (!empty($_SERVER[$k])) $out[strtolower($n)] ??= [$n, (string)$_SERVER[$k]];
    return $out;
}
function client_ip(array $h): string {
    $x = $h['x-forwarded-for'][1] ?? '';
    return $x !== '' ? trim(explode(',', $x)[0]) : (string)($_SERVER['REMOTE_ADDR'] ?? '');
}
function valid_id(string $id): bool { return (bool)preg_match('/^[a-f0-9]{24}$/', $id); }
function cleanup(string $id): void {
    foreach (["/d/$id.json", "/b/$id", "/r/$id", "/s/$id", "/c/$id", "/m/$id.dlv", "/m/$id.ack"] as $f) @unlink(SPOOL . $f);
}
function gc(): void {
    if (random_int(0, 200) !== 0) return;
    $limit = time() - 3600;
    foreach (['q', 'd', 'b', 'r', 'c', 's', 'm'] as $d) foreach ((array)glob(SPOOL . "/$d/*") as $f) if (is_string($f) && @filemtime($f) < $limit) @unlink($f);
}
function alive_age(): float { clearstatcache(true, SPOOL . '/alive'); $t = @filemtime(SPOOL . '/alive'); return $t ? time() - $t : 1e9; }
function write_atomic(string $path, string $data): void { $t = $path . '.' . bin2hex(random_bytes(3)) . '.tmp'; file_put_contents($t, $data); rename($t, $path); }
function read_rec(string $path): ?array { $s = @file_get_contents($path); if ($s === false || $s === '') return null; $j = json_decode($s, true); return is_array($j) ? $j : null; }

// ------------------------------------------------------------------ küme uçları
function h_poll(array $h): void {
    $in = json_decode((string)file_get_contents('php://input'), true) ?: [];
    touch(SPOOL . '/alive');
    $active = array_flip(array_map('strval', $in['active'] ?? []));
    foreach (($in['ack'] ?? []) as $id) {
        $id = (string)$id; if (!valid_id($id)) continue;
        $active[$id] = 1;
        if (is_file(SPOOL . "/d/$id.json") && !is_file(SPOOL . "/m/$id.ack")) @touch(SPOOL . "/m/$id.ack");
    }
    clearstatcache();
    $now = time();
    foreach ((array)glob(SPOOL . '/m/*.ack') as $p) {            // küme yeniden başladıysa bekleyen istek askıda kalmasın
        $id = basename((string)$p, '.ack'); $t = (int)@filemtime((string)$p);
        if ($t && $t < $now - 3 && !isset($active[$id]) && is_file(SPOOL . "/b/$id")) {
            append_frames($id, [[['i' => $id, 's' => PHP_INT_MAX, 't' => 'x', 'm' => 'küme isteği kaybetti'], '']], true);
            @unlink((string)$p);
        }
    }
    $deadline = microtime(true) + min(25.0, (float)($in['wait'] ?? 20));
    $limit = min(64, (int)($in['max'] ?? 16));
    $items = []; $cancel = [];
    while (true) {
        clearstatcache();
        $q = glob(SPOOL . '/q/*.json') ?: []; sort($q);
        foreach ($q as $f) {
            if (count($items) >= $limit) break;
            $id = substr(basename($f, '.json'), -24);
            $to = SPOOL . "/d/$id.json";
            if (!@rename($f, $to)) continue;                     // başka bir yoklama kaptı
            @touch(SPOOL . "/m/$id.dlv");
            $r = read_rec($to); if ($r) $items[$id] = $r;
        }
        foreach ((array)glob(SPOOL . '/d/*.json') as $p) {
            if (count($items) >= $limit) break;
            $id = basename((string)$p, '.json');
            if (isset($items[$id]) || is_file(SPOOL . "/m/$id.ack")) continue;
            if (time() - (int)@filemtime(SPOOL . "/m/$id.dlv") > REDELIVER_S) { @touch(SPOOL . "/m/$id.dlv"); $r = read_rec((string)$p); if ($r) $items[$id] = $r; }
        }
        foreach ((array)glob(SPOOL . '/c/*') as $c) { $cancel[] = basename((string)$c); @unlink((string)$c); }
        if ($items || $cancel || microtime(true) >= $deadline) break;
        usleep(25000);
        touch(SPOOL . '/alive');
    }
    touch(SPOOL . '/alive');
    $reqs = [];
    foreach ($items as $id => $r) {
        $n = (int)@filesize(SPOOL . "/b/$id");
        $reqs[] = ['id' => $id, 'm' => $r['m'], 'p' => $r['p'], 'q' => $r['q'], 'h' => $r['h'], 'ip' => $r['ip'], 'n' => $n,
                   'b' => $n <= INLINE_BODY ? base64_encode((string)@file_get_contents(SPOOL . "/b/$id")) : null];
    }
    jout(200, ['v' => 1, 'requests' => $reqs, 'cancel' => $cancel, 't' => microtime(true), 'ip' => client_ip($h)]);
}
function encode_frame(array $hd, string $data): string { $j = json_encode($hd, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES); return pack('N', strlen($j)) . $j . pack('N', strlen($data)) . $data; }
function append_frames(string $id, array $frames, bool $force = false): void {
    $path = SPOOL . "/r/$id"; $sp = SPOOL . "/s/$id";
    $fh = @fopen($path, 'ab'); if (!$fh) return;
    flock($fh, LOCK_EX);
    $last = (int)(@file_get_contents($sp) === false ? -1 : @file_get_contents($sp));
    $out = '';
    foreach ($frames as [$hd, $data]) { if ($hd['s'] <= $last && !$force) continue; if ($hd['s'] !== PHP_INT_MAX) $last = $hd['s']; $out .= encode_frame($hd, $data); }
    if ($out !== '') { fwrite($fh, $out); fflush($fh); @file_put_contents($sp, (string)$last); }
    flock($fh, LOCK_UN); fclose($fh);
}
function h_push(): void {
    $raw = (string)file_get_contents('php://input');
    $body = substr($raw, 1);
    if (($raw[0] ?? "\0") === "\x01") { $body = @gzuncompress($body); if ($body === false) { fail(400, 'Bozuk paket', 'bad_frame'); return; } }
    $off = 0; $n = strlen($body); $by = [];
    while ($off + 4 <= $n) {
        $hl = unpack('N', substr($body, $off, 4))[1]; $off += 4;
        $hd = json_decode(substr($body, $off, $hl), true); $off += $hl;
        $dl = unpack('N', substr($body, $off, 4))[1]; $off += 4;
        $data = substr($body, $off, $dl); $off += $dl;
        if (is_array($hd) && valid_id((string)($hd['i'] ?? ''))) $by[$hd['i']][] = [$hd, $data];
    }
    foreach ($by as $id => $frames) if (is_file(SPOOL . "/b/$id")) append_frames((string)$id, $frames);
    $cancel = [];
    foreach ((array)glob(SPOOL . '/c/*') as $c) { $cancel[] = basename((string)$c); @unlink((string)$c); }
    jout(200, ['ok' => true, 'cancel' => $cancel]);
}
function h_body(): void {
    $id = (string)($_GET['id'] ?? ''); $p = SPOOL . "/b/$id";
    if (!valid_id($id) || !is_file($p)) { fail(404, 'İstek yok', 'not_found'); return; }
    header('Content-Type: application/octet-stream'); header('Content-Length: ' . filesize($p)); readfile($p);
}

// ------------------------------------------------------------------ site uçları
function h_public(string $path, array $h): void {
    $method = (string)($_SERVER['REQUEST_METHOD'] ?? 'GET');
    if ($method !== 'OPTIONS' && empty($h['authorization'][1]) && empty($h['x-api-key'][1])) { fail(401, 'Kimlik doğrulama gerekli (Authorization: Bearer <anahtar>)', 'missing_api_key'); return; }
    if ((int)($_SERVER['CONTENT_LENGTH'] ?? 0) > MAX_BODY) { fail(413, 'İstek çok büyük', 'too_large'); return; }
    if (alive_age() > ALIVE_S) { fail(503, 'Küme şu anda bağlı değil', 'cluster_offline'); return; }
    $id = bin2hex(random_bytes(12));
    $out = fopen(SPOOL . "/b/$id", 'wb');
    if (stripos($h['content-type'][1] ?? '', 'multipart/form-data') === 0) {       // PHP çok parçalı gövdeyi kendisi ayrıştırır: yeniden kurulur
        if (!$_FILES && !$_POST && (int)($_SERVER['CONTENT_LENGTH'] ?? 0) > 0) { fclose($out); @unlink(SPOOL . "/b/$id"); fail(413, 'Dosya barındırmanın yükleme sınırını aşıyor (post_max_size)', 'too_large'); return; }
        $bd = 'kume' . bin2hex(random_bytes(10)); $n = 0;
        foreach ($_POST as $k => $v) if (is_scalar($v)) $n += (int)fwrite($out, "--$bd\r\nContent-Disposition: form-data; name=\"$k\"\r\n\r\n$v\r\n");
        foreach ($_FILES as $k => $f) {
            if (!is_string($f['tmp_name'] ?? null) || (int)$f['error'] !== UPLOAD_ERR_OK) { fclose($out); @unlink(SPOOL . "/b/$id"); fail(413, 'Dosya yüklenemedi (barındırma sınırı: upload_max_filesize)', 'too_large'); return; }
            $name = str_replace('"', '', (string)$f['name']); $type = (string)($f['type'] ?: 'application/octet-stream');
            $n += (int)fwrite($out, "--$bd\r\nContent-Disposition: form-data; name=\"$k\"; filename=\"$name\"\r\nContent-Type: $type\r\n\r\n");
            $src = fopen($f['tmp_name'], 'rb'); $n += (int)stream_copy_to_stream($src, $out); fclose($src); $n += (int)fwrite($out, "\r\n");
        }
        $n += (int)fwrite($out, "--$bd--\r\n");
        $h['content-type'] = ['Content-Type', "multipart/form-data; boundary=$bd"];
    } else {
        $in = fopen('php://input', 'rb'); $n = stream_copy_to_stream($in, $out, MAX_BODY + 1); fclose($in);
    }
    fclose($out);
    if ($n > MAX_BODY) { @unlink(SPOOL . "/b/$id"); fail(413, 'İstek çok büyük', 'too_large'); return; }
    $hop = ['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'];
    $hd = []; foreach ($h as $k => [$name, $v]) if (!in_array($k, $hop, true)) $hd[] = [$name, $v];
    $rec = ['id' => $id, 'm' => $method, 'p' => $path, 'q' => (string)($_SERVER['QUERY_STRING'] ?? ''), 'h' => $hd, 'ip' => client_ip($h), 'delivered' => 0];
    write_atomic(SPOOL . sprintf('/q/%016.0f-%s.json', microtime(true) * 1000, $id), json_encode($rec));
    while (ob_get_level()) @ob_end_clean();
    $resp = SPOOL . "/r/$id"; $fh = null; $off = 0; $started = false; $deadline = microtime(true) + HEAD_WAIT; $buf = ''; $last = microtime(true);
    try {
        while (true) {
            if (connection_aborted()) { @touch(SPOOL . "/c/$id"); return; }
            if (!$fh) { clearstatcache(true, $resp); if (is_file($resp)) $fh = @fopen($resp, 'rb'); }
            $progress = false;
            if ($fh) {
                fseek($fh, $off); $chunk = fread($fh, 1048576);
                if ($chunk !== false && $chunk !== '') { $buf .= $chunk; $off += strlen($chunk); }
                while (true) {
                    $len = strlen($buf); if ($len < 4) break;
                    $hl = unpack('N', substr($buf, 0, 4))[1]; if ($len < 8 + $hl) break;
                    $dl = unpack('N', substr($buf, 4 + $hl, 4))[1]; if ($len < 8 + $hl + $dl) break;
                    $f = json_decode(substr($buf, 4, $hl), true); $data = substr($buf, 8 + $hl, $dl); $buf = substr($buf, 8 + $hl + $dl); $progress = true; $last = microtime(true);
                    $t = $f['t'] ?? '';
                    if ($t === 'h') {
                        $started = true; http_response_code((int)$f['st']);
                        foreach ($f['hd'] ?? [] as [$k, $v]) { $l = strtolower($k); if ($l !== 'content-length' && in_array($l, $hop, true)) continue; header("$k: $v", false); }
                        header('X-Accel-Buffering: no'); header('X-LiteSpeed-Cache-Control: no-cache'); header('Cache-Control: no-cache'); flush();
                    } elseif ($t === 'd') { echo $data; flush(); }
                    elseif ($t === 'e') { flush(); return; }
                    elseif ($t === 'x') { if (!$started) fail(502, (string)($f['m'] ?? 'Küme hatası'), 'cluster_error'); return; }
                }
            }
            if (!$started && microtime(true) > $deadline) { fail(504, 'Küme zamanında yanıt vermedi', 'cluster_timeout'); @touch(SPOOL . "/c/$id"); return; }
            if ($started && microtime(true) - $last > 900) return;     // küme uzun süredir sessiz: PHP işçisini boşa tutma
            if (!$progress) usleep(15000);
        }
    } finally {
        if ($fh) fclose($fh);
        foreach ((array)glob(SPOOL . "/q/*-$id.json") as $q) @unlink((string)$q);
        $cancelled = is_file(SPOOL . "/c/$id"); cleanup($id); if ($cancelled) @touch(SPOOL . "/c/$id");
    }
}

// ------------------------------------------------------------------ yönlendirme
if (strlen(TUNNEL_SECRET) < 16 || strncmp(TUNNEL_SECRET, '__', 2) === 0) { fail(500, 'Röle yapılandırılmamış: TUNNEL_SECRET', 'not_configured'); exit; }
spool_ready(); gc();
$path = (string)($_SERVER['PATH_INFO'] ?? '');
if ($path === '') { $u = (string)parse_url((string)($_SERVER['REQUEST_URI'] ?? ''), PHP_URL_PATH); $s = (string)($_SERVER['SCRIPT_NAME'] ?? ''); $path = strncmp($u, $s, strlen($s)) === 0 ? substr($u, strlen($s)) : $u; }
$h = req_headers();
if (($i = strpos($path, '/__tunnel/')) !== false) {
    $a = substr($path, $i + 10);
    $auth = $h['authorization'][1] ?? ''; $key = strncasecmp($auth, 'Bearer ', 7) === 0 ? substr($auth, 7) : ($h['x-tunnel-key'][1] ?? '');
    if (!hash_equals(TUNNEL_SECRET, (string)$key)) { fail(401, 'Geçersiz tünel anahtarı', 'bad_tunnel_key'); exit; }
    $m = (string)($_SERVER['REQUEST_METHOD'] ?? 'GET');
    if ($a === 'poll' && $m === 'POST') h_poll($h); elseif ($a === 'push' && $m === 'POST') h_push(); elseif ($a === 'body' && $m === 'GET') h_body(); else fail(404, 'Bulunamadı', 'not_found');
    exit;
}
if (substr($path, -8) === '/healthz' || $path === '' || $path === '/') { $age = alive_age(); jout(200, ['status' => 'ok', 'cluster' => $age <= ALIVE_S, 'age_s' => $age > 1e8 ? null : $age]); exit; }
if (($i = strpos($path, '/v1/')) === false) { fail(404, 'Bulunamadı', 'not_found'); exit; }
h_public(substr($path, $i), $h);
