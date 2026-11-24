#!/usr/bin/env bash
# Cài hoặc cập nhật Knowledge Agent trên Ubuntu. Chạy bằng user có sudo.
# Dùng: sudo DOMAIN=example.org REPO_DIR=/path/to/source bash deploy/install.sh
set -euo pipefail

DOMAIN="${DOMAIN:?thiếu DOMAIN}"
REPO_DIR="${REPO_DIR:?thiếu REPO_DIR}"
APP_DIR=/opt/aiops
ENV_FILE=/etc/aiops/aiops.env

apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl debian-keyring debian-archive-keyring apt-transport-https gnupg

if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi

id aiops >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin aiops
install -d -o aiops -g aiops -m 750 "$APP_DIR" "$APP_DIR/data"
install -d -m 750 -o root -g aiops /etc/aiops

tar -C "$REPO_DIR" -cf - aiops requirements.txt | tar -C "$APP_DIR" -xf -
chown -R root:aiops "$APP_DIR/aiops" "$APP_DIR/requirements.txt"
chmod -R g+rX,o-rwx "$APP_DIR/aiops"

[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R aiops:aiops "$APP_DIR/.venv"

if [ ! -f "$ENV_FILE" ]; then
  umask 027
  cat > "$ENV_FILE" <<EOF
AIOPS_DATA_DIR=$APP_DIR/data
AIOPS_COOKIE_SECURE=1
# Điền khóa để bật Claude, rồi: sudo systemctl restart aiops
ANTHROPIC_API_KEY=
EOF
  chown root:aiops "$ENV_FILE"
  chmod 640 "$ENV_FILE"
fi

# Khóa chủ mã hóa LLM key: nằm ngoài thư mục dữ liệu để bản sao DB không kèm khóa giải mã.
if ! grep -q '^AIOPS_SECRET_KEY=' "$ENV_FILE"; then
  echo "AIOPS_SECRET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')" >> "$ENV_FILE"
fi

install -m 644 "$REPO_DIR/deploy/aiops.service" /etc/systemd/system/aiops.service
sed "s/__DOMAIN__/$DOMAIN/g" "$REPO_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable --quiet aiops caddy
systemctl restart aiops
systemctl reload-or-restart caddy
systemctl is-active aiops caddy
