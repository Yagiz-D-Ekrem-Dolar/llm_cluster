#!/usr/bin/env bash
# Ana PC Ubuntu ise kontrol sunucusunu (panel + ağ geçidi + model deposu) kurar.
#
#   sudo bash deploy/install_controller.sh --admin-password 'güçlü-parola' --no-caddy
#
# Site bağlantısı panelden kurulur (Site bağlantısı sayfası): küme hosting'e kendisi bağlandığı için modemde port açmak gerekmez.
# Panel ve dosya dağıtım portu yalnızca yerel ağda dinler; ağ geçidi 127.0.0.1'dedir.
#
# Seçenekler:
#   --admin-password P   yönetici parolası (zorunlu, en az 8 karakter)
#   --no-caddy           HTTPS vekili kurma (önerilen: röle ya da Cloudflare Tunnel kullanılır)
#   --domain D           yalnızca modemde gerçek IP ve port yönlendirme varsa: Caddy kurulur, D -> 127.0.0.1:<ağ geçidi>
#   --panel-host IP      panelin dinleyeceği yerel ağ adresi (varsayılan: bu makinenin yerel ağ IP'si)
#   --panel-port N       varsayılan 8080          --public-port N   varsayılan 8081    --fabric-port N   varsayılan 8079
#   --dir D              uygulama klasörü (varsayılan /opt/orkestra-controller)
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/orkestra-controller; DATA_DIR=/var/lib/orkestra-controller; SVC_USER=orkestra-ctl
ADMIN_PW="${ORKESTRA_ADMIN_PASSWORD:-}"; DOMAIN=""; PANEL_HOST=""; PANEL_PORT=8080; PUBLIC_PORT=8081; FABRIC_PORT=8079; NO_CADDY=0

die() { echo "HATA: $*" >&2; exit 1; }
say() { printf '\n\033[1;34m[%s]\033[0m %s\n' "$1" "$2"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --admin-password) ADMIN_PW="$2"; shift 2;;
    --domain) DOMAIN="$2"; shift 2;;
    --no-caddy) NO_CADDY=1; shift;;
    --panel-host) PANEL_HOST="$2"; shift 2;;
    --panel-port) PANEL_PORT="$2"; shift 2;;
    --public-port) PUBLIC_PORT="$2"; shift 2;;
    --fabric-port) FABRIC_PORT="$2"; shift 2;;
    --dir) APP_DIR="$2"; shift 2;;
    *) die "bilinmeyen seçenek: $1";;
  esac
done
[ "$(id -u)" = 0 ] || die "root olarak çalıştırın (sudo bash ...)"
[ "${#ADMIN_PW}" -ge 8 ] || die "--admin-password en az 8 karakter olmalı"
[ -f "$SRC_DIR/run.py" ] || die "proje klasörü bulunamadı ($SRC_DIR)"
if [ -z "$PANEL_HOST" ]; then
  PANEL_HOST="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
fi
[ -n "$PANEL_HOST" ] || die "yerel ağ adresi bulunamadı; --panel-host verin"

PUBLIC_HOST="127.0.0.1"

say "1/6" "Paketler"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv rsync curl iproute2 git >/dev/null

say "2/6" "Kullanıcı ve klasörler"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SVC_USER"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$DATA_DIR"
install -d -m 0755 "$APP_DIR"

say "3/6" "Uygulama ve Python bağımlılıkları"
rsync -a --delete --exclude data --exclude __pycache__ --exclude '*.pyc' --exclude tests --exclude .claude --exclude venv "$SRC_DIR/" "$APP_DIR/"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

say "4/6" "Yapılandırma ve servis"
ESC_PW="$(printf '%s' "$ADMIN_PW" | sed 's/\\/\\\\/g; s/"/\\"/g')"
( umask 077; printf 'ORKESTRA_ADMIN_PASSWORD="%s"\n' "$ESC_PW" > /etc/orkestra-controller.env )
cat > /etc/systemd/system/orkestra-controller.service <<EOF
[Unit]
Description=LLM cluster controller
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
EnvironmentFile=/etc/orkestra-controller.env
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/run.py --host $PANEL_HOST --port $PANEL_PORT --public-host $PUBLIC_HOST --public-port $PUBLIC_PORT --fabric-port $FABRIC_PORT --data-dir $DATA_DIR
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now orkestra-controller.service
systemctl restart orkestra-controller.service

say "5/6" "Güvenlik duvarı"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  CIDR="$(ip -o -4 addr show | awk -v ip="$PANEL_HOST" '$4 ~ "^"ip"/" {print $4; exit}')"
  SRC="any"; [ -n "$CIDR" ] && SRC="$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$CIDR")"
  ufw allow from "$SRC" to any port "$PANEL_PORT" proto tcp >/dev/null
  ufw allow from "$SRC" to any port "$FABRIC_PORT" proto tcp >/dev/null
  [ -n "$DOMAIN" ] && { ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; }
  echo "  ufw: panel ve dosya dağıtımı yalnızca $SRC adresinden açıldı${DOMAIN:+; 80/443 genel}"
else
  echo "  ufw etkin değil; $PANEL_PORT ve $FABRIC_PORT portlarını internete açmayın."
fi

say "6/6" "HTTPS ters vekil"
if [ -n "$DOMAIN" ] && [ "$NO_CADDY" = 0 ]; then
  if ! command -v caddy >/dev/null 2>&1; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy >/dev/null
  fi
  cat > /etc/caddy/orkestra.caddy <<EOF
$DOMAIN {
    reverse_proxy 127.0.0.1:$PUBLIC_PORT {
        flush_interval -1
        transport http {
            response_header_timeout 15m
            read_timeout 15m
        }
    }
    request_body {
        max_size 50MB
    }
}
EOF
  grep -qF "import /etc/caddy/orkestra.caddy" /etc/caddy/Caddyfile 2>/dev/null || echo "import /etc/caddy/orkestra.caddy" >> /etc/caddy/Caddyfile
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
  echo "  Caddy: https://$DOMAIN -> 127.0.0.1:$PUBLIC_PORT (DNS A kaydı bu makinenin genel IP'sine işaret etmeli; 80/443 yönlendirilmeli)"
else
  echo "  Atlandı: siteye bağlantıyı panelden kurun (Site bağlantısı)."
fi

sleep 2
echo
echo "=== Ana PC kuruldu ==="
systemctl is-active orkestra-controller.service >/dev/null && echo "Servis : çalışıyor (orkestra-controller)" || echo "Servis : ÇALIŞMIYOR → journalctl -u orkestra-controller -n 50"
echo "Panel  : http://$PANEL_HOST:$PANEL_PORT   (yalnızca yerel ağ)   Parola: verdiğiniz --admin-password"
echo
echo "Sonraki adım: panelde Sunucular → Ağı tara ve kur, ardından Site bağlantısı."
