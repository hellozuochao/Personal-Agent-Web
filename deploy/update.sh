#!/bin/bash
# 自动更新（codeload 通路版）：广州服务器直连 github.com 的 git 通路不通
# （TLS 连接被重置，2026-10-21 实测三次失败），改走 GitHub 官方源码包下载
# （codeload.github.com，同日实测可用）。以仓库根目录 VERSION 文件对账：
# 包里版本号与本机不同才部署。由 always-on-update.timer 每 5 分钟触发，root 运行。
# 本脚本安装在 /usr/local/bin/always-on-update.sh 运行，避免更新时覆盖正在执行的自己。
set -u
APP_DIR="${APP_DIR:-/opt/always-on}"
REPO_TARBALL="https://codeload.github.com/hellozuochao/Personal-Agent-Web/tar.gz/refs/heads/main"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LOG="$APP_DIR/update.log"
saylog() { echo "$(date -Is) $*" >> "$LOG" 2>/dev/null || true; }

# 防重入：定时器和远程即时触发可能撞在一起，同一时刻只跑一个更新
exec 9>/tmp/always-on-update.lock
flock -n 9 || exit 0

curl -sL --retry 2 --max-time 120 -o "$TMP/repo.tar.gz" "$REPO_TARBALL" \
  || { saylog "codeload 下载失败"; exit 0; }
tar xzf "$TMP/repo.tar.gz" -C "$TMP" 2>/dev/null \
  || { saylog "源码包解压失败"; exit 0; }
SRC="$TMP/Personal-Agent-Web-main"
[ -d "$SRC" ] || { saylog "源码包结构异常（没有预期的目录）"; exit 0; }
NEW_VERSION="$(cat "$SRC/VERSION" 2>/dev/null || echo "")"
OLD_VERSION="$(cat "$APP_DIR/VERSION" 2>/dev/null || echo "")"
[ -z "$NEW_VERSION" ] && { saylog "源码包里没有 VERSION 文件"; exit 0; }
# 手动触发（/admin/update）会先落一个标记：这种 run 把远端版本记进日志，
# 事后能分清"脚本没跑"还是"跑了但拉到的是旧包"（2026-10-21 两次卡版的教训）。
if [ -f /tmp/always-on-update.mark ]; then
  MARK_AGE=$(( $(date +%s) - $(stat -c %Y /tmp/always-on-update.mark 2>/dev/null || echo 0) ))
  if [ "$MARK_AGE" -lt 600 ]; then
    saylog "手动触发检查：远端 $NEW_VERSION / 本机 ${OLD_VERSION:-无}"
    rm -f /tmp/always-on-update.mark
  fi
fi
[ "$NEW_VERSION" = "$OLD_VERSION" ] && exit 0

# 部署：包内只有代码文件；运行时文件（state.json / approvals.json / 日志）不在包内，不受影响
cp -a "$SRC"/. "$APP_DIR"/
chown -R ubuntu:ubuntu "$APP_DIR"
echo "$NEW_VERSION" > "$APP_DIR/.deployed_commit"
systemctl restart always-on-agent always-on-api
# 更新脚本自刷新（下次生效）
cp "$APP_DIR/deploy/update.sh" /usr/local/bin/always-on-update.sh 2>/dev/null || true
chmod +x /usr/local/bin/always-on-update.sh 2>/dev/null || true
echo "$(date -Is) 已更新 ${OLD_VERSION:-无} -> $NEW_VERSION 并重启服务" >> "$APP_DIR/update.log"
