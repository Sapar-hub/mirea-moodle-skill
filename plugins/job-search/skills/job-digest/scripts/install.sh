#!/usr/bin/env bash
# Ставит job-digest: RSSHub (docker), конфиг, systemd --user timer.
#   ./install.sh                  # всё по умолчанию: дайджест каждый день в 09:13
#   ONCALENDAR='*-*-* 09,19:13' ./install.sh
#   RSSHUB_PROXY=http://127.0.0.1:18080 ./install.sh   # если t.me открывается только через прокси
#   ./install.sh uninstall
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
CONF=${JOB_DIGEST_CONFIG:-$HOME/.config/job-digest}
UNIT_DIR=$HOME/.config/systemd/user
ONCALENDAR=${ONCALENDAR:-'*-*-* 09:13'}
RSSHUB_PROXY=${RSSHUB_PROXY:-${HTTPS_PROXY:-${https_proxy:-}}}

if [ "${1:-}" = uninstall ]; then
  systemctl --user disable --now job-digest.timer 2>/dev/null || true
  rm -f "$UNIT_DIR"/job-digest.{service,timer}
  systemctl --user daemon-reload
  docker rm -f rsshub 2>/dev/null || true
  echo "OK: удалено (конфиг $CONF и база ~/.local/state/job-digest оставлены)"
  exit 0
fi

# 1. RSSHub: сеть хоста, чтобы был виден локальный прокси; слушает только 127.0.0.1
if ! docker ps --format '{{.Names}}' | grep -qx rsshub; then
  docker rm -f rsshub 2>/dev/null || true
  docker run -d --name rsshub --restart unless-stopped --network host \
    -e LISTEN_INADDR_ANY=0 -e CACHE_EXPIRE=600 \
    ${RSSHUB_PROXY:+-e PROXY_URI="$RSSHUB_PROXY"} \
    diygod/rsshub:latest >/dev/null
  echo "OK: rsshub запущен${RSSHUB_PROXY:+ (прокси $RSSHUB_PROXY)}"
fi

# 2. конфиг: не перетираем то, что уже правили руками
mkdir -p "$CONF"
for f in feeds.txt keywords.txt mail-rules.json; do
  [ -e "$CONF/$f" ] || cp "$HERE/defaults/$f" "$CONF/$f"
done

# 3. пароль: из fish universal vars (если они там есть), иначе из $CONF/env (chmod 600)
RUN="/usr/bin/env python3 -I $HERE/scripts/digest.py run"
SORT="/usr/bin/env python3 -I $HERE/scripts/mailsort.py"
if command -v fish >/dev/null && fish -c 'set -q MAILRU_USER; and set -q MAILRU_APPPASS'; then
  EXEC="$(command -v fish) -c 'exec $RUN'"
  EXEC_SORT="$(command -v fish) -c 'exec $SORT'"
else
  [ -e "$CONF/env" ] || { printf 'MAILRU_USER=\nMAILRU_APPPASS=\n' > "$CONF/env"; chmod 600 "$CONF/env"; }
  EXEC="$RUN"
  EXEC_SORT="$SORT"
  grep -q '^MAILRU_APPPASS=.' "$CONF/env" || echo "WARN: заполни $CONF/env (пароль для внешних приложений Mail.ru)"
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/job-digest.service" <<EOF
[Unit]
Description=Job digest: RSS -> Mail.ru
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=-$CONF/env
ExecStart=$EXEC
ExecStartPost=$EXEC_SORT
EOF
cat > "$UNIT_DIR/job-digest.timer" <<EOF
[Unit]
Description=Job digest schedule

[Timer]
OnCalendar=$ONCALENDAR
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now job-digest.timer >/dev/null
echo "OK: timer ($ONCALENDAR). Следующий запуск:"
systemctl --user list-timers job-digest.timer --no-pager | sed -n 2p
