#!/usr/bin/env bash
# Runs ON the prod VM (copied there by .github/workflows/deploy.yml over the IAP tunnel).
#
# Idempotent: ensure the repo + venv exist, fast-forward to the deployed commit, (re)install the
# systemd unit, and restart ONLY the FastAPI webhook service. It deliberately does NOT touch the
# emulator, frida-server, the toyota-frida bypass service, or the logged-in Toyota session — those
# keep running across a deploy, so a code push never risks the live rig's app state.
#
# Inputs (env, set by the deploy workflow's ssh --command):
#   APP_USER    - the VM user that owns the emulator/adb; the service runs as this user  (required)
#   REPO_URL    - https clone URL of this repo (public)                                   (required)
#   DEPLOY_SHA  - exact commit to deploy; falls back to origin/master if empty            (optional)
#   APP_DIR     - where the code lives on the VM (default /opt/toyota-sms-remote)         (optional)
#   PORT        - port uvicorn binds on localhost (default 8080)                          (optional)
set -euo pipefail

APP_USER="${APP_USER:?APP_USER not set}"
REPO_URL="${REPO_URL:?REPO_URL not set}"
DEPLOY_SHA="${DEPLOY_SHA:-}"
APP_DIR="${APP_DIR:-/opt/toyota-sms-remote}"
PORT="${PORT:-8080}"
SERVICE="toyota-sms"

echo "==> Deploying ${REPO_URL} @ ${DEPLOY_SHA:-origin/master} -> ${APP_DIR} (service user: ${APP_USER})"

# 1) code dir, owned by the app user
sudo mkdir -p "$APP_DIR"
sudo chown "$APP_USER:$APP_USER" "$APP_DIR"

# 2) sync code + python env AS the app user, so everything is owned correctly
sudo -u "$APP_USER" env REPO_URL="$REPO_URL" APP_DIR="$APP_DIR" DEPLOY_SHA="$DEPLOY_SHA" bash <<'ASUSER'
set -euo pipefail
if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
git remote set-url origin "$REPO_URL"
git fetch --prune origin
if [ -n "$DEPLOY_SHA" ]; then
  git checkout -f "$DEPLOY_SHA"
else
  git checkout -f origin/master
fi
cd "$APP_DIR/server"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
ASUSER

# 3) (re)write the systemd unit, rendered from env so nothing user/path-specific is committed.
#    EnvironmentFile has a leading '-' so it's optional: the service still starts before a real
#    .env (with the TextGrid webhook secret + allowlist) is placed at $APP_DIR/server/.env.
sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<UNIT
[Unit]
Description=Toyota SMS Remote - TextGrid webhook (FastAPI). UI-only control of the Toyota app.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}/server
EnvironmentFile=-${APP_DIR}/server/.env
ExecStart=${APP_DIR}/server/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# 4) restart ONLY the webhook service (emulator / frida / app session are left running)
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE}.service"
sudo systemctl restart "${SERVICE}.service"
sleep 2
sudo systemctl --no-pager --full status "${SERVICE}.service" | head -n 15 || true

# 5) smoke check — /health returns 200 even with no emulator attached (device_online just reads false)
if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null; then
  echo "==> /health OK — deploy complete."
else
  echo "==> WARNING: /health did not respond. Inspect: journalctl -u ${SERVICE} -n 50 --no-pager"
  exit 1
fi
