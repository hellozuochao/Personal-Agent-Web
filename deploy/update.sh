#!/bin/bash
# 自动更新：远端有新提交就拉取并重启服务。
# 由 always-on-update.timer 每 5 分钟触发，以 root 运行；
# git 操作切到 ubuntu（仓库属主），避免 dubious ownership 报错。
set -u
APP_DIR=/opt/always-on
RUN="runuser -u ubuntu --"

cd "$APP_DIR" || exit 1
if ! $RUN git fetch origin main --quiet; then
  echo "$(date -Is) fetch 失败" >> "$APP_DIR/update.log"
  exit 0
fi
LOCAL=$($RUN git rev-parse HEAD)
REMOTE=$($RUN git rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi
if $RUN git pull --ff-only --quiet; then
  $RUN git rev-parse HEAD > "$APP_DIR/.deployed_commit"
  systemctl restart always-on-agent always-on-api
  echo "$(date -Is) 已更新 $LOCAL -> $REMOTE 并重启服务" >> "$APP_DIR/update.log"
else
  echo "$(date -Is) pull 失败（本地与远端分叉？）$LOCAL -> $REMOTE" >> "$APP_DIR/update.log"
fi
