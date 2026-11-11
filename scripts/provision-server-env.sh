#!/usr/bin/env bash
# 在服务器上执行（root）：把 .env.server.example 变成可用的 .env，并保证 MySQL 账号与之一致。
#
#   bash scripts/provision-server-env.sh
#
# 设计要点：
#   - 密钥全部在服务器本机用 openssl 生成，不打印、不进命令行进程参数（避免 ps 泄露）；
#   - 只用 CREATE USER IF NOT EXISTS / ALTER USER：不 DROP、不碰已有库里的数据；
#   - DATABASE_URL 由 DB_USER + DB_PASSWORD + DB_NAME 每次重算写回，
#     保证「MySQL 里的密码」和「容器读到的密码」永远同步；
#   - 已存在的 .env 只补 __GENERATE__ 项，手工改过的值（SMTP 等）一律保留。
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DB_NAME="${DB_NAME:-downloadany}"
DB_USER="${DB_USER:-downloadany_api}"
ENV_FILE="${APP_DIR}/.env"

# 已存在的 .env 默认只补缺失项；--rotate-secrets 会把 JWT_SECRET/DB_PASSWORD
# 重置成占位符重新生成（已签发的访问令牌会立刻失效、MySQL 应用账号会改密，
# 适用于密钥疑似泄露到聊天记录/日志这类情况）。
ROTATE=0
for arg in "$@"; do
  case "$arg" in
    --rotate-secrets) ROTATE=1 ;;
    *) printf '未知参数：%s\n' "$arg" >&2; exit 2 ;;
  esac
done

cd "$APP_DIR"
umask 077

say() { printf '\033[36m==\033[0m %s\n' "$*"; }
die() { printf '\033[31m!!\033[0m %s\n' "$*" >&2; exit 1; }

get() { awk -F= -v k="$1" "index(\$0, k\"=\")==1 { sub(\"^[^=]*=\", \"\"); print; exit }" "$ENV_FILE"; }

# 覆盖式写键：先删同名再追加，绕开 sed 的转义坑
put() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  grep -v "^${key}=" "$ENV_FILE" >"$tmp" || true
  printf '%s=%s\n' "$key" "$val" >>"$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

is_placeholder() { [ -z "$1" ] || [[ "$1" == __GENERATE__* ]] || [ "$1" = "generate" ]; }

say "1/4 准备 ${ENV_FILE}"
[ -f "$ENV_FILE" ] || cp .env.server.example "$ENV_FILE"

if [ "$ROTATE" = 1 ]; then
  say "  --rotate-secrets：把两个密钥重置为占位符，下面重新生成"
  put JWT_SECRET generate
  put DB_PASSWORD generate
fi

if is_placeholder "$(get JWT_SECRET || true)"; then
  say "  生成 JWT_SECRET（64 位十六进制，只落在服务器上的 .env）"
  put JWT_SECRET "$(openssl rand -hex 32)"
else
  say "  JWT_SECRET 已存在，保留"
fi

DB_PASSWORD="$(get DB_PASSWORD || true)"
if is_placeholder "$DB_PASSWORD"; then
  say "  生成 MySQL 应用账号密码（只落在服务器上的 .env）"
  DB_PASSWORD="$(openssl rand -hex 24)"
  put DB_PASSWORD "$DB_PASSWORD"
else
  say "  DB_PASSWORD 已存在，沿用它并对齐 MySQL 账号"
fi

say "2/4 检查宿主机 MySQL（root 走 auth_socket 免密）"
command -v mysql >/dev/null || die "服务器上找不到 mysql 客户端"
mysql -uroot -N -e "SELECT 1" >/dev/null 2>&1 \
  || die "mysql -uroot 连不上：root 不是 auth_socket 认证，请提供可用的管理账号后再跑本脚本"

mysql -uroot <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
SQL
say "  数据库 ${DB_NAME} 就绪（既有表不动，缺表由后端启动时 create_all 补建）"

# 只授权 localhost/127.0.0.1：后端容器用 host 网络，连接源始终是回环，不向任何公网网卡开放
mysql -uroot <<SQL
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASSWORD}';
CREATE USER IF NOT EXISTS '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASSWORD}';
ALTER USER '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASSWORD}';
ALTER USER '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASSWORD}';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'localhost';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
say "3/4 MySQL 账号 ${DB_USER}@localhost/127.0.0.1 已就绪"

put DATABASE_URL "mysql+pymysql://${DB_USER}:${DB_PASSWORD}@127.0.0.1:3306/${DB_NAME}?charset=utf8mb4"

# 用临时 defaults-file 真连一次（密码不进 ps 参数），只报告结果
CNF="$(mktemp)"
trap 'rm -f "$CNF"' EXIT
printf '[client]\nhost=127.0.0.1\nuser=%s\npassword=%s\ndatabase=%s\n' \
  "$DB_USER" "$DB_PASSWORD" "$DB_NAME" >"$CNF"
chmod 600 "$CNF"

TABLES="$(mysql --defaults-extra-file="$CNF" -N -e \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${DB_NAME}'" 2>&1)" \
  || die "应用账号连不上 MySQL：检查 .env 的 DB_PASSWORD 与 ${DB_USER} 账号是否一致"
say "4/4 应用账号连接正常，${DB_NAME} 现有 ${TABLES} 张表"

say "完成。下一步：docker compose -f docker-compose.server.yml up -d --build"
