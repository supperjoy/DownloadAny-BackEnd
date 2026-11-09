#!/usr/bin/env bash
# 本机 → 服务器一键部署后端（宿主机 MySQL + 仅回环监听 + SSH 隧道连调）
#
#   SERVER=root@101.96.199.191 bash scripts/deploy-server.sh [--with-swap] [--rotate-secrets]
#
# 做四件事：
#   1) rsync 源码到 ${REMOTE_DIR}（.env / *.db / .venv / tests 一律不传）
#   2) 在服务器上跑 provision-server-env.sh：生成密钥、建/对齐 MySQL 应用账号
#   3) docker compose -f docker-compose.server.yml up -d --build
#   4) 服务器本机 curl 127.0.0.1:8000/health 验收
#
# --with-swap：若服务器没有 swap，创建 2G swapfile 并写进 /etc/fstab（2G 内存的机器
# 跑 docker build 很容易被 OOM killer 打断，首次部署建议带上）。
set -euo pipefail

SERVER="${SERVER:-root@101.96.199.191}"
REMOTE_DIR="${REMOTE_DIR:-/opt/downloadany-backend}"
COMPOSE_FILE="docker-compose.server.yml"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WITH_SWAP=0
PROVISION_ARGS=""
for arg in "$@"; do
  case "$arg" in
    --with-swap) WITH_SWAP=1 ;;
    --rotate-secrets) PROVISION_ARGS="$PROVISION_ARGS --rotate-secrets" ;;
    *) printf '未知参数：%s（可用：--with-swap / --rotate-secrets）\n' "$arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[36m==\033[0m %s\n' "$*"; }
die() { printf '\033[31m!!\033[0m %s\n' "$*" >&2; exit 1; }

say "0/5 连通性检查：ssh ${SERVER}"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER" 'echo ok >/dev/null' \
  || die "ssh 免密登录失败。请先执行：ssh-copy-id ${SERVER}"

if [ "$WITH_SWAP" = 1 ]; then
  say "1/5 确保 2G swap"
  ssh "$SERVER" '
    set -e
    if [ -n "$(swapon --show --noheadings)" ]; then
      echo "  已有 swap，跳过"
    else
      fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null
      swapon /swapfile
      grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >>/etc/fstab
      echo "  已启用 /swapfile 并写入 /etc/fstab"
    fi
    free -m | awk "/Swap/ {print \"  swap 总量：\" \$2 \" MB\"}"'
else
  say "1/5 跳过 swap 配置（需要时加 --with-swap）"
fi

say "2/5 同步源码 → ${SERVER}:${REMOTE_DIR}"
ssh "$SERVER" "mkdir -p ${REMOTE_DIR}"
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.py[cod]' \
  --exclude '.pytest_cache/' \
  --exclude '.env' \
  --exclude '.env.local' \
  --exclude '*.db' \
  --exclude 'tests/' \
  --exclude 'uvicorn*.log' \
  "${LOCAL_DIR}/" "${SERVER}:${REMOTE_DIR}/"

say "3/5 服务器上准备 .env 与 MySQL 账号"
ssh "$SERVER" "APP_DIR=${REMOTE_DIR} bash ${REMOTE_DIR}/scripts/provision-server-env.sh${PROVISION_ARGS}"

say "4/5 构建并启动容器"
ssh "$SERVER" "cd ${REMOTE_DIR} && docker compose -f ${COMPOSE_FILE} up -d --build" \
  || die "构建/启动失败，看日志：ssh ${SERVER} 'cd ${REMOTE_DIR} && docker compose -f ${COMPOSE_FILE} logs --tail=80 backend'"

say "5/5 健康检查"
ssh "$SERVER" '
  for i in $(seq 1 20); do
    body=$(curl -fsS -m 3 http://127.0.0.1:8000/health 2>/dev/null) && { echo "  ${body}"; exit 0; }
    sleep 2
  done
  echo "  /health 未就绪" >&2
  docker compose -f '"${COMPOSE_FILE}"' logs --tail=40 backend >&2
  exit 1'

printf '\n部署完成。本机连调请开一个常驻终端跑隧道：\n'
printf '  ssh -N -o ExitOnForwardFailure=yes -L 8000:127.0.0.1:8000 %s\n' "$SERVER"
printf '前端保持 VITE_API_BASE_URL=http://127.0.0.1:8000 即可（CSP/CORS 无需改动）。\n'
printf '没配 SMTP 时验证码在容器日志里：ssh %s "cd %s && docker compose -f %s logs --since=2m backend"\n' \
  "$SERVER" "$REMOTE_DIR" "$COMPOSE_FILE"
