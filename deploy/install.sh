#!/usr/bin/env bash
# Cài hoặc cập nhật Knowledge Agent trên Ubuntu. Chạy bằng user có sudo.
# Dùng: sudo DOMAIN=example.org REPO_DIR=/path/to/source bash deploy/install.sh
set -euo pipefail

DOMAIN="${DOMAIN:?thiếu DOMAIN}"
REPO_DIR="${REPO_DIR:?thiếu REPO_DIR}"
APP_DIR=/opt/kagent
ENV_FILE=/etc/kagent/kagent.env

apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl debian-keyring debian-archive-keyring apt-transport-https gnupg

if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi

id kagent >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin kagent
install -d -o kagent -g kagent -m 750 "$APP_DIR" "$APP_DIR/data"
install -d -m 750 -o root -g kagent /etc/kagent

tar -C "$REPO_DIR" -cf - kagent requirements.txt | tar -C "$APP_DIR" -xf -
chown -R root:kagent "$APP_DIR/kagent" "$APP_DIR/requirements.txt"
chmod -R g+rX,o-rwx "$APP_DIR/kagent"

[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R kagent:kagent "$APP_DIR/.venv"

if [ ! -f "$ENV_FILE" ]; then
  umask 027
  cat > "$ENV_FILE" <<EOF
KAGENT_DATA_DIR=$APP_DIR/data
KAGENT_COOKIE_SECURE=1
# Điền khóa để bật Claude, rồi: sudo systemctl restart kagent
ANTHROPIC_API_KEY=
EOF
  chown root:kagent "$ENV_FILE"
  chmod 640 "$ENV_FILE"
fi

install -m 644 "$REPO_DIR/deploy/kagent.service" /etc/systemd/system/kagent.service
sed "s/__DOMAIN__/$DOMAIN/g" "$REPO_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable --quiet kagent caddy
systemctl restart kagent
systemctl reload-or-restart caddy
systemctl is-active kagent caddy
