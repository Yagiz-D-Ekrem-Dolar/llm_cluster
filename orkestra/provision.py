"""Makine kurulumu: ana PC'den SSH ile bağlanıp ajanı switch üzerinden gönderir ve kurar.

Parola yalnızca kurulum süresince bellekte tutulur; diske ya da günlüğe yazılmaz. Kurulumdan sonra
makine ajan üzerinden yönetilir, SSH gerekmez. Ana PC kendi GPU'sunu da yerel ajanla kümeye katabilir.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

from .fabric import AGENT_FILE, _lan_ip_for
from .util import jdump, now, rand_token

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "agent" / "install_agent.sh"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class Provisioner:
    def __init__(self, ctx):
        self.ctx, self.db = ctx, ctx.db
        self.jobs: OrderedDict[str, dict] = OrderedDict()
        self._bg: set[asyncio.Task] = set()
        self.local_proc: subprocess.Popen | None = None

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def stop(self) -> None:
        for t in list(self._bg):
            t.cancel()

    def view(self, j: dict, tail: int = 400) -> dict:
        return {**{k: v for k, v in j.items() if not k.startswith("_") and k != "log"}, "log": j["log"][-tail:]}

    def list(self) -> list[dict]:
        return [self.view(j, 30) for j in reversed(self.jobs.values())]

    def start(self, req: dict) -> dict:
        host = str(req.get("host", "")).strip()
        user = str(req.get("user", "")).strip()
        if not re.match(r"^[A-Za-z0-9.-]{1,253}$", host):
            raise ValueError("Geçersiz adres")
        if not re.match(r"^[a-z_][a-z0-9_.-]{0,31}$", user):
            raise ValueError("Geçersiz kullanıcı adı")
        if any(j["host"] == host and j["state"] == "running" for j in self.jobs.values()):
            raise ValueError(f"{host} için kurulum zaten sürüyor")
        jid = rand_token(6)
        job = {"id": jid, "host": host, "user": user, "state": "running", "step": "bağlanılıyor", "log": [], "node_id": None, "name": str(req.get("name") or "").strip(),
               "error": "", "t0": now(), "t1": 0, "reboot": False}
        self.jobs[jid] = job
        while len(self.jobs) > 40:
            self.jobs.popitem(last=False)
        self._spawn(self._run(job, req))
        return self.view(job)

    def _log(self, job: dict, line: str, secrets=()) -> None:
        line = ANSI.sub("", line).rstrip("\r\n")
        for s in secrets:
            if s:
                line = line.replace(s, "•••")
        if line.strip():
            job["log"].append(line[:400])
            del job["log"][:-1500]

    async def _run(self, job: dict, req: dict) -> None:
        password = str(req.get("password") or "")
        key_text = str(req.get("private_key") or "")
        token = None
        try:
            if self.ctx.sim:
                await self._simulate(job)
                return
            import asyncssh
            existing = next((n for n in self.ctx.cluster.nodes.values() if n.host == job["host"]), None)
            token = existing.agent_token if existing else rand_token(24)
            secrets = (password, token)
            port = int(req.get("ssh_port") or 22)
            agent_port = int(req.get("agent_port") or (existing.agent_port if existing else 7777))
            kw = dict(host=job["host"], port=port, username=job["user"], known_hosts=None, connect_timeout=12, login_timeout=20)
            if key_text:
                kw["client_keys"] = [asyncssh.import_private_key(key_text, password or None)]
            elif password:
                kw.update(password=password, client_keys=None)
            self._log(job, f"{job['user']}@{job['host']}:{port} bağlantısı açılıyor")
            async with asyncssh.connect(**kw) as conn:
                fp = conn.get_server_host_key()
                if fp:
                    self._log(job, f"makine anahtarı: {fp.get_fingerprint()}")
                job["step"] = "makine denetleniyor"
                r = await conn.run('uname -s; uname -m; id -u; hostname; . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-?}"', check=False)
                info = (r.stdout or "").splitlines()
                if len(info) < 5 or info[0].strip() != "Linux":
                    raise ValueError("Bu makine Linux değil ya da komut çalıştırılamadı")
                uid, hostname, os_name = info[2].strip(), info[3].strip(), info[4].strip()
                self._log(job, f"{hostname} · {os_name} · {info[1].strip()}")
                sudo = ""
                if uid != "0":
                    r = await conn.run("sudo -S -p '' true", input=password + "\n", check=False)
                    if r.exit_status != 0:
                        raise PermissionError("sudo yetkisi alınamadı: kullanıcı yönetici değil ya da parola yanlış")
                    sudo = "sudo -S -p '' "
                job["step"] = "dosyalar gönderiliyor"
                tmp = (await conn.run("mktemp -d /tmp/kurulum.XXXXXX", check=True)).stdout.strip()
                try:
                    env_text = f"TOKEN={token}\n"
                    async with conn.start_sftp_client() as sftp:
                        await sftp.put(str(AGENT_FILE), f"{tmp}/node_agent.py")
                        await sftp.put(str(INSTALLER), f"{tmp}/install_agent.sh")
                        async with sftp.open(f"{tmp}/install.env", "w") as f:
                            await f.write(env_text)
                        await sftp.chmod(f"{tmp}/install.env", 0o600)
                    self._log(job, "ajan ve kurulum betiği gönderildi")
                    ctrl = f"http://{_lan_ip_for(job['host'])}:{self.ctx.boot.admin_port}"
                    args = ["--controller", ctrl, "--agent-file", f"{tmp}/node_agent.py", "--env-file", f"{tmp}/install.env", "--port", str(agent_port)]
                    for flag, opt in (("--with-build-tools", "build_tools"), ("--with-ffmpeg", "ffmpeg"), ("--with-piper", "piper"), ("--gpu-driver", "gpu_driver")):
                        if req.get(opt):
                            args.append(flag)
                    cmd = f"{sudo}bash {tmp}/install_agent.sh " + " ".join(shlex.quote(a) for a in args)
                    job["step"] = "kuruluyor"
                    proc = await conn.create_process(cmd, stderr=asyncssh.STDOUT, encoding="utf-8", errors="replace")
                    if sudo:
                        proc.stdin.write(password + "\n")
                    proc.stdin.write_eof()
                    async for line in proc.stdout:
                        self._log(job, line, secrets)
                        if "yeniden başlatma gerekli" in line:
                            job["reboot"] = True
                    res = await proc.wait(check=False)
                    if res.exit_status != 0:
                        raise IOError(f"kurulum betiği hata verdi (çıkış {res.exit_status})")
                finally:
                    await conn.run(f"rm -rf {shlex.quote(tmp)}", check=False)
            job["step"] = "ajan bekleniyor"
            nid = self._register(job, existing, token, agent_port, hostname)
            n = await self._wait_online(nid, 45)
            if not n:
                raise IOError("Ajan kuruldu ama yanıt vermedi (güvenlik duvarı?)")
            job.update(state="done", step="tamamlandı", t1=now(), node_id=nid, name=n.name)
            self._log(job, f"{n.name} kümeye katıldı")
            self.db.event("info", "node", f"{n.name} ({job['host']}) ağdan kuruldu", nid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            msg = f"{e}" or type(e).__name__
            if type(e).__name__ in ("PermissionDenied",):
                msg = "SSH girişi reddedildi: kullanıcı adı ya da parola yanlış"
            for s in (password, token):
                if s:
                    msg = msg.replace(s, "•••")
            job.update(state="failed", error=msg[:300], t1=now())
            self._log(job, "HATA: " + msg)
            self.db.event("error", "node", f"{job['host']} kurulamadı: {msg[:160]}")
        finally:
            password = key_text = ""

    def _register(self, job: dict, existing, token: str, agent_port: int, hostname: str) -> int:
        db = self.db
        if existing:
            db.x("UPDATE nodes SET agent_port=?, ssh_user=? WHERE id=?", (agent_port, job["user"], existing.id))
            self.ctx.cluster.reload()
            return existing.id
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", job["name"] or hostname or job["host"])[:40] or job["host"]
        name, i = base, 2
        while db.one("SELECT id FROM nodes WHERE name=?", (name,)):
            name, i = f"{base}-{i}", i + 1
        nid = db.x("INSERT INTO nodes(name,host,agent_port,agent_token,role,tags,ssh_user,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (name, job["host"], agent_port, token, "laptop", jdump([]), job["user"], now()))
        self.ctx.cluster.reload()
        return nid

    async def _wait_online(self, nid: int, timeout: float):
        c = self.ctx.cluster
        end = now() + timeout
        while now() < end:
            n = c.nodes.get(nid)
            if n and n.status == "online" and n.hw:
                if not n.hw.get("laptop") and n.role == "laptop":
                    self.db.x("UPDATE nodes SET role='server' WHERE id=?", (nid,))
                    c.reload()
                return n
            if n:
                n.next_poll = 0.0
            await asyncio.sleep(1.0)
        return None

    async def _simulate(self, job: dict) -> None:
        for step, line in (("makine denetleniyor", "ubuntu-sunucu · Ubuntu 24.04 LTS · x86_64"), ("dosyalar gönderiliyor", "ajan ve kurulum betiği gönderildi"),
                           ("kuruluyor", "[1/7] Paketler"), ("kuruluyor", "[6/7] systemd servisi"), ("ajan bekleniyor", "Servis : çalışıyor")):
            job["step"] = step
            self._log(job, line)
            await asyncio.sleep(0.7)
        job.update(state="failed", error="Demo modunda gerçek makine kurulamaz", t1=now())
        self._log(job, "Demo modu: gerçek kurulum için uygulamayı normal modda başlatın")

    # ---- ana PC'yi de kümeye katma ----------------------------------------------
    def add_self(self, port: int = 7777) -> int:
        """Bu bilgisayarda yerel ajan başlatır; GPU'su kümeye eklenir, uyutulmaz."""
        if self.ctx.sim:
            raise ValueError("Demo modunda kullanılamaz")
        info = self.ctx.network.info
        host = info.get("ip") or "127.0.0.1"
        row = self.db.one("SELECT * FROM nodes WHERE host=? OR host='127.0.0.1'", (host,))
        token = row["agent_token"] if row else rand_token(24)
        state = Path(self.ctx.data_dir) / "self-agent"
        state.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "ORKESTRA_AGENT_TOKEN": token, "ORKESTRA_AGENT_PORT": str(port), "ORKESTRA_STATE_DIR": str(state)}
        flags = {}
        if os.name == "nt":
            flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            flags["start_new_session"] = True
        if self.local_proc is None or self.local_proc.poll() is not None:
            log_f = open(state / "agent.log", "ab")
            self.local_proc = subprocess.Popen([sys.executable, str(AGENT_FILE)], env=env, stdout=log_f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **flags)
        if row:
            return row["id"]
        base, name, i = "ana-pc", "ana-pc", 2
        while self.db.one("SELECT id FROM nodes WHERE name=?", (name,)):
            name, i = f"{base}-{i}", i + 1
        nid = self.db.x("INSERT INTO nodes(name,host,agent_port,agent_token,role,tags,allow_sleep,allow_restart,always_on,created_at) VALUES(?,?,?,?,?,?,0,0,1,?)",
                        (name, host, port, token, "main", jdump(["main"]), now()))
        self.ctx.cluster.reload()
        self.db.event("info", "node", f"Bu bilgisayar kümeye eklendi: {name}", nid)
        return nid

    def resume_self(self) -> None:
        """Kontrol sunucusu yeniden başlarken yerel ajanı da ayağa kaldırır."""
        if self.ctx.sim:
            return
        row = self.db.one("SELECT * FROM nodes WHERE role='main' AND sim=0 LIMIT 1")
        if row and (Path(self.ctx.data_dir) / "self-agent").is_dir():
            try:
                import socket
                with socket.create_connection(("127.0.0.1", row["agent_port"]), 0.3):
                    return
            except OSError:
                self.add_self(row["agent_port"])
