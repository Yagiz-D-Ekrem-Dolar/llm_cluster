"""Sınama için SSH sunucusu: Ubuntu makinenin yerine geçer.

Gerçek SSH protokolü (parola girişi, SFTP, komut çalıştırma). Komutlar Git Bash ile çalışır; apt-get, systemctl, useradd gibi
sistem komutları sahte betiklerdir, ama `systemctl enable --now orkestra-agent` gerçek ajanı başlatır.
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncssh

host, port, user, password, sandbox, bash, agent_host = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], Path(sys.argv[5]), sys.argv[6], sys.argv[7]
STUBS = sandbox / "stubs"
ROOT = sandbox / "root"
MSYS_TMP = subprocess.run([bash, "-c", "cygpath -m /tmp"], capture_output=True, text=True).stdout.strip()


def stub(name, body):
    (STUBS / name).write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8", newline="\n")


STUBS.mkdir(parents=True, exist_ok=True)
for n in ("apt-get", "useradd", "usermod", "chown", "chgrp", "visudo", "ethtool", "nmcli", "ubuntu-drivers"):
    stub(n, "exit 0")
stub("getent", "exit 1")
stub("uname", 'case "${1:-}" in -m) echo x86_64;; *) echo Linux;; esac')
stub("lspci", "echo '00:02.0 VGA compatible controller: Intel'")
stub("id", 'if [ "$1" = "-u" ] && [ -z "${2:-}" ]; then [ -n "${AS_ROOT:-}" ] && echo 0 || echo 1000; elif [ "$1" = "-u" ]; then exit 1; else echo uid=0; fi')
stub("sudo", 'IFS= read -r pw || true; [ "$pw" = "$SSH_TEST_PASSWORD" ] || { echo "sudo: yanlış parola" >&2; exit 1; }\nshift 2 2>/dev/null; [ "${1:-}" = "" ] && shift; AS_ROOT=1 exec "$@"')
stub("install", 'd=0; args=(); while [ $# -gt 0 ]; do case "$1" in -d) d=1; shift;; -m|-o|-g) shift 2;; *) args+=("$1"); shift;; esac; done\nmkdir -p "${args[@]}"')
stub("python3", f'exec "{Path(sys.executable).as_posix()}" "$@"')
stub("ip", 'case "$*" in "route show default") echo "default via 127.0.0.254 dev testnet0";; *addr*) echo "2: testnet0 inet 127.0.1.9/24";; esac')
stub("systemctl", r'''
case "$*" in
  *"--now orkestra-agent.service"*|"restart orkestra-agent.service")
    "$(dirname "$0")/start-agent"
    ;;
esac
exit 0''')
stub("start-agent", f'''
set -a; . <(sed 's/\\r$//' "{ROOT.as_posix()}/etc/orkestra/agent.env" | grep -v '^HOME='); set +a
export ORKESTRA_AGENT_HOST="{agent_host}" ORKESTRA_STORE_DIR="{(ROOT / 'store').as_posix()}" ORKESTRA_STATE_DIR="{(ROOT / 'state').as_posix()}" TEST_BIND="{agent_host}"
[ -f "{(sandbox / 'agent.pid').as_posix()}" ] && kill "$(cat "{(sandbox / 'agent.pid').as_posix()}")" 2>/dev/null
nohup "{Path(sys.executable).as_posix()}" "{(ROOT / 'opt/orkestra/node_agent.py').as_posix()}" --dry-power >>"{(sandbox / 'agent.log').as_posix()}" 2>&1 &
echo $! > "{(sandbox / 'agent.pid').as_posix()}"
sleep 1''')
STUBS_U = subprocess.run([bash, "-c", f"cygpath -u '{STUBS.as_posix()}'"], capture_output=True, text=True).stdout.strip()
ENV = {**os.environ, "ORKESTRA_ROOT": ROOT.as_posix(), "SSH_TEST_PASSWORD": password, "MSYS": "winsymlinks:lnk"}


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, pw):
        return username == user and pw == password


class Sftp(asyncssh.SFTPServer):
    def map_path(self, path):
        p = path.decode() if isinstance(path, bytes) else path
        if p.startswith("/tmp/"):
            p = MSYS_TMP + p[4:]
        return p.encode() if isinstance(path, bytes) else p


async def on_process(process):
    cmd = f'export PATH="{STUBS_U}:$PATH"; {process.command}'   # Git Bash kendi klasörlerini öne alır
    p = await asyncio.create_subprocess_exec(bash, "-c", cmd, env=ENV, stdin=asyncio.subprocess.PIPE,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    async def out(src, dst):
        while True:
            b = await src.read(65536)
            if not b:
                return
            dst.write(b)
            await dst.drain()

    async def into():
        try:
            while True:
                b = await process.stdin.read(65536)
                if not b:
                    break
                p.stdin.write(b)
                await p.stdin.drain()
        except Exception:
            pass
        try:
            p.stdin.close()
        except Exception:
            pass

    feeder = asyncio.create_task(into())
    await asyncio.gather(out(p.stdout, process.stdout), out(p.stderr, process.stderr))
    code = await p.wait()
    feeder.cancel()
    process.exit(code)


async def main():
    key = asyncssh.generate_private_key("ssh-ed25519")
    await asyncssh.listen(host, port, server_factory=Server, server_host_keys=[key], process_factory=on_process, sftp_factory=Sftp, encoding=None)
    print("ssh hazır", flush=True)
    await asyncio.Event().wait()


asyncio.run(main())
