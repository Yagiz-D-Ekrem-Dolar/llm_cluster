#!/usr/bin/env bash
# Makine ajanı kurulumu (Ubuntu / Debian). Normalde panel bunu SSH ile switch üzerinden kendisi çalıştırır:
#   Sunucular → Ağı tara ve kur
#
# Elle kurulum:
#   curl -fsSL http://ANA-PC:8080/agent/install.sh | sudo bash -s -- --controller http://ANA-PC:8080 --token TOKEN
#
# Yaptıkları: python3/ethtool/lm-sensors kurar, 'orkestra' sistem kullanıcısı açar, ajanı /opt/orkestra'ya koyar,
# model ve runtime deposunu hazırlar, systemd servisi kurar, yalnızca güç komutları için sudo izni verir,
# Wake-on-LAN'ı açar (kalıcı), dizüstülerde kapak kapanınca uykuya geçmeyi engeller, ufw açıksa yerel ağa port açar.
#
# Seçenekler: --with-ffmpeg (ses biçimleri) · --with-piper (Türkçe TTS) · --with-build-tools (yamalı llama.cpp'yi makinede derlemek için)
#             --gpu-driver (NVIDIA sürücüsü yoksa kurar; yeniden başlatma gerekir)
set -euo pipefail

R="${ORKESTRA_ROOT:-}"   # yalnızca sınama için: dosyaları başka köke yazar
CONTROLLER=""; TOKEN=""; PORT=7777; USER_NAME="orkestra"; NO_WOL=0; LID_IGNORE=1; ALLOW_FROM=""; EXTRA_PATH=""
ALLOW_EXE=""; ALLOW_DIRS=""; AGENT_FILE=""; ENV_FILE=""; WITH_BUILD=0; WITH_FFMPEG=0; WITH_PIPER=0; GPU_DRIVER=0
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-exe) ALLOW_EXE="${ALLOW_EXE:+$ALLOW_EXE,}$2"; shift 2;;
    --runtime-dir) ALLOW_DIRS="${ALLOW_DIRS:+$ALLOW_DIRS,}$2"; shift 2;;
    --controller) CONTROLLER="${2%/}"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --user) USER_NAME="$2"; shift 2;;
    --no-wol) NO_WOL=1; shift;;
    --keep-lid) LID_IGNORE=0; shift;;
    --allow-from) ALLOW_FROM="$2"; shift 2;;
    --extra-path) EXTRA_PATH="$2"; shift 2;;
    --agent-file) AGENT_FILE="$2"; shift 2;;
    --env-file) ENV_FILE="$2"; shift 2;;
    --with-build-tools) WITH_BUILD=1; shift;;
    --with-ffmpeg) WITH_FFMPEG=1; shift;;
    --with-piper) WITH_PIPER=1; shift;;
    --gpu-driver) GPU_DRIVER=1; shift;;
    *) echo "Bilinmeyen seçenek: $1" >&2; exit 2;;
  esac
done
if [ -n "$ENV_FILE" ]; then
  [ -r "$ENV_FILE" ] || { echo "okunamadı: $ENV_FILE" >&2; exit 2; }
  TOKEN="$(sed -n 's/^TOKEN=//p' "$ENV_FILE" | head -1)"
fi
[ -n "$TOKEN" ] || { echo "--token gerekli" >&2; exit 2; }
[ -n "$CONTROLLER" ] || [ -n "$AGENT_FILE" ] || { echo "--controller ya da --agent-file gerekli" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root olarak çalıştırın (sudo bash ...)" >&2; exit 1; }

say() { printf '\n[%s] %s\n' "$1" "$2"; }
warn() { echo "  ! $*"; }

say "1/8" "Paketler"
export DEBIAN_FRONTEND=noninteractive
PKGS="python3 curl ethtool lm-sensors pciutils iproute2"
[ "$WITH_FFMPEG" = 1 ] && PKGS="$PKGS ffmpeg"
[ "$WITH_PIPER" = 1 ] && PKGS="$PKGS python3-venv"
[ "$WITH_BUILD" = 1 ] && PKGS="$PKGS build-essential cmake ninja-build pkg-config git libcurl4-openssl-dev"
apt-get update -qq || warn "paket listesi güncellenemedi (internet?)"
# shellcheck disable=SC2086
apt-get install -y -qq $PKGS >/dev/null || warn "bazı paketler kurulamadı: $PKGS"
command -v python3 >/dev/null || { echo "python3 yok ve kurulamadı" >&2; exit 1; }
PY3="$(command -v python3)"

say "2/8" "GPU"
if lspci 2>/dev/null | grep -qi nvidia; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | sed 's/^/  /' || true
    [ "$WITH_BUILD" = 1 ] && { apt-get install -y -qq nvidia-cuda-toolkit >/dev/null 2>&1 || warn "CUDA araç seti kurulamadı (kaynaktan CUDA derlemesi için gerekir)"; }
  elif [ "$GPU_DRIVER" = 1 ]; then
    apt-get install -y -qq ubuntu-drivers-common >/dev/null || true
    if ubuntu-drivers install; then echo "  NVIDIA sürücüsü kuruldu: yeniden başlatma gerekli"; else warn "NVIDIA sürücüsü kurulamadı"; fi
  else
    warn "NVIDIA GPU var ama sürücü çalışmıyor (kurulumda 'GPU sürücüsü' seçeneğini açın)"
  fi
else
  echo "  NVIDIA GPU bulunmadı"
fi

say "3/8" "Kullanıcı ve klasörler"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/orkestra --shell /usr/sbin/nologin "$USER_NAME"
for g in video render docker; do getent group "$g" >/dev/null && usermod -aG "$g" "$USER_NAME" || true; done
install -d -m 0755 -o "$USER_NAME" "$R/opt/orkestra"
install -d -m 0750 -o "$USER_NAME" "$R/var/lib/orkestra" "$R/var/lib/orkestra/store"

say "4/8" "Ajan dosyası"
if [ -n "$AGENT_FILE" ]; then
  cp "$AGENT_FILE" "$R/opt/orkestra/node_agent.py"
else
  curl -fsSL "$CONTROLLER/agent/node_agent.py" -o "$R/opt/orkestra/node_agent.py"
fi
chmod 0755 "$R/opt/orkestra/node_agent.py"
chown "$USER_NAME" "$R/opt/orkestra/node_agent.py"   # ajan kendini güncelleyebilsin
"$PY3" -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" "$R/opt/orkestra/node_agent.py"

say "5/8" "Yapılandırma"
install -d -m 0750 -o root -g "$USER_NAME" "$R/etc/orkestra"
( umask 027
  cat > "$R/etc/orkestra/agent.env" <<EOF
ORKESTRA_AGENT_TOKEN=$TOKEN
ORKESTRA_AGENT_PORT=$PORT
ORKESTRA_ALLOW_FROM=$ALLOW_FROM
ORKESTRA_EXTRA_PATH=$EXTRA_PATH
ORKESTRA_ALLOW_EXE=$ALLOW_EXE
ORKESTRA_ALLOW_DIRS=$ALLOW_DIRS
ORKESTRA_STORE_DIR=/var/lib/orkestra/store
HOME=/var/lib/orkestra
EOF
)
chgrp "$USER_NAME" "$R/etc/orkestra/agent.env"; chmod 0640 "$R/etc/orkestra/agent.env"
install -d "$R/usr/local/bin"
cat > "$R/usr/local/bin/orkestra-agent-allow" <<'EOS'
#!/usr/bin/env bash
# Ajana panelin dağıtmadığı bir çalıştırılabilir dosyayı (tam yol) ya da klasörü izinli ekler.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo ile çalıştırın" >&2; exit 1; }
[ $# -ge 1 ] || { echo "kullanım: sudo orkestra-agent-allow <yol|ad> [...]" >&2; exit 2; }
ENVF=/etc/orkestra/agent.env
for p in "$@"; do
  if [ -d "$p" ]; then KEY=ORKESTRA_ALLOW_DIRS; else KEY=ORKESTRA_ALLOW_EXE; fi
  cur="$(grep -E "^$KEY=" "$ENVF" | head -1 | cut -d= -f2- || true)"
  case ",$cur," in *",$p,"*) echo "zaten izinli: $p"; continue;; esac
  new="${cur:+$cur,}$p"
  if grep -qE "^$KEY=" "$ENVF"; then sed -i "s|^$KEY=.*|$KEY=$new|" "$ENVF"; else echo "$KEY=$new" >> "$ENVF"; fi
  echo "izin verildi: $p"
done
systemctl restart orkestra-agent
EOS
chmod 0755 "$R/usr/local/bin/orkestra-agent-allow"

say "6/8" "Güç izni ve ek bileşenler"
install -d "$R/etc/sudoers.d"
cat > "$R/etc/sudoers.d/orkestra" <<EOF
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl suspend, /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff, /bin/systemctl suspend, /bin/systemctl reboot, /bin/systemctl poweroff
EOF
chmod 0440 "$R/etc/sudoers.d/orkestra"
visudo -cf "$R/etc/sudoers.d/orkestra" >/dev/null
if [ "$WITH_PIPER" = 1 ]; then
  if "$PY3" -m venv "$R/opt/orkestra/piper" && "$R/opt/orkestra/piper/bin/pip" install -q --upgrade pip piper-tts; then
    chown -R "$USER_NAME" "$R/opt/orkestra/piper"; echo "  Piper kuruldu"
  else warn "Piper kurulamadı (internet?)"; fi
fi

say "7/8" "systemd servisi"
install -d "$R/etc/systemd/system"
cat > "$R/etc/systemd/system/orkestra-agent.service" <<EOF
[Unit]
Description=LLM cluster node agent
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
EnvironmentFile=/etc/orkestra/agent.env
ExecStart=$PY3 /opt/orkestra/node_agent.py
Restart=always
RestartSec=3
LimitNOFILE=65536
LimitMEMLOCK=infinity
# Ajan yeniden başlasa da çıkarım süreçleri çalışmaya devam etsin
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now orkestra-agent.service
systemctl restart orkestra-agent.service

say "8/8" "Ağ: Wake-on-LAN, kapak, güvenlik duvarı"
IFACE="$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')"
MAC=""; IPCIDR=""
if [ -n "$IFACE" ]; then
  MAC="$(cat "/sys/class/net/$IFACE/address" 2>/dev/null || true)"
  IPCIDR="$(ip -o -4 addr show dev "$IFACE" 2>/dev/null | awk '{print $4; exit}')"
  [ -d "/sys/class/net/$IFACE/wireless" ] && warn "$IFACE kablosuz: makineyi kabloyla switch'e bağlayın (Wake-on-LAN ve hız için)"
  SPEED="$(cat "/sys/class/net/$IFACE/speed" 2>/dev/null || echo 0)"
  { [ "${SPEED:-0}" -gt 0 ] && [ "${SPEED:-0}" -lt 1000 ]; } 2>/dev/null && warn "$IFACE bağlantı hızı ${SPEED} Mb/sn: kabloyu ya da switch portunu kontrol edin"
fi
if [ "$NO_WOL" = 0 ] && [ -n "$IFACE" ]; then
  ETHTOOL="$(command -v ethtool || echo /usr/sbin/ethtool)"
  "$ETHTOOL" -s "$IFACE" wol g 2>/dev/null || warn "$IFACE için Wake-on-LAN açılamadı"
  cat > "$R/etc/systemd/system/orkestra-wol.service" <<EOF
[Unit]
Description=Wake-on-LAN ($IFACE)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$ETHTOOL -s $IFACE wol g

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now orkestra-wol.service >/dev/null 2>&1 || true
  # uykudan/bağlantı kopmasından sonra NetworkManager ayarı sıfırlamasın
  if command -v nmcli >/dev/null 2>&1; then
    CONN="$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v d="$IFACE" '$2==d {print $1; exit}')"
    [ -n "${CONN:-}" ] && nmcli connection modify "$CONN" 802-3-ethernet.wake-on-lan magic >/dev/null 2>&1 || true
  fi
fi
if [ "$LID_IGNORE" = 1 ]; then
  install -d "$R/etc/systemd/logind.conf.d"
  cat > "$R/etc/systemd/logind.conf.d/orkestra.conf" <<EOF
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF
  systemctl kill -s HUP systemd-logind >/dev/null 2>&1 || true
fi
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  SRC="${ALLOW_FROM:-}"
  if [ -z "$SRC" ] && [ -n "$IPCIDR" ]; then
    SRC="$("$PY3" -c 'import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$IPCIDR")"
  fi
  SRC="${SRC:-any}"
  ufw allow from "$SRC" to any port "$PORT" proto tcp >/dev/null
  ufw allow from "$SRC" to any port 18000:18999 proto tcp >/dev/null
  echo "  ufw: $SRC adresinden $PORT ve 18000-18999 portlarına izin verildi"
fi

sleep 1
echo
echo "=== Ajan kuruldu ==="
systemctl is-active orkestra-agent.service >/dev/null && echo "Servis : çalışıyor" || echo "Servis : ÇALIŞMIYOR → journalctl -u orkestra-agent -n 50"
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && echo "Sağlık : tamam" || echo "Sağlık : ajan henüz yanıt vermedi"
echo "IP     : ${IPCIDR:-bilinmiyor}   Arayüz: ${IFACE:-?}   MAC: ${MAC:-bilinmiyor}"
echo "BIOS/UEFI'de 'Wake on LAN' açık olmalı; dizüstülerde yalnızca kablolu bağlantı güvenilirdir."
