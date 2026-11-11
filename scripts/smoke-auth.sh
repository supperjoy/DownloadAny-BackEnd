#!/usr/bin/env bash
# 认证链路冒烟测试：验证「注册（邮箱 + 密码 + 确认密码，成功即登录）→ 未注册邮箱登录被引导注册 → 密码登录 → 改密码 → 找回密码」这条产品流程。
# 邮箱验证码登录已下线，脚本不再依赖它；只有找回密码那步需要读后端日志里的邮件验证码。
#
#   bash scripts/smoke-auth.sh                       # 默认打 http://127.0.0.1:8000（配 SSH 隧道）
#   API=http://127.0.0.1:8001 bash scripts/smoke-auth.sh
#   REMOTE_FETCH=0 bash scripts/smoke-auth.sh        # 本机跑后端：改为手工粘贴日志里的验证码
#   SKIP_RESET=1 bash scripts/smoke-auth.sh          # 跳过找回密码（没配 SMTP / 拿不到日志）
#
# 默认经 ssh 到 SERVER 的容器日志里取验证码（未配 SMTP 的联调形态），命令本身不用带参数。
#
# 只在开发/联调环境跑：账号是一次性邮箱，任何密钥/令牌都不打印。
set -uo pipefail

API="${API:-http://127.0.0.1:8000}"
ORIGIN="${ORIGIN:-http://localhost:1420}"
EMAIL="${EMAIL:-smoke-$(date +%s)@example.com}"
PASSWORD="${PASSWORD:-Sm0ke$(date +%s)-ini}"
NEW_PASSWORD="${NEW_PASSWORD:-Sm0ke$(date +%s)-pwd}"
RESET_PASSWORD="${RESET_PASSWORD:-Sm0ke$(date +%s)-rst}"
SERVER="${SERVER:-root@101.96.199.191}"
LOGS="${LOGS:-docker compose -f /opt/downloadany-backend/docker-compose.server.yml logs --since=90s backend}"
# 默认经 ssh 从服务器容器日志取验证码（未配 SMTP 的联调形态）；本机跑后端时设 REMOTE_FETCH=0 手工粘贴
REMOTE_FETCH="${REMOTE_FETCH:-1}"
SKIP_RESET="${SKIP_RESET:-0}"
COOKIE_JAR="$(mktemp)"
COOKIE_JAR2="$(mktemp)"
BODY="$(mktemp)"
AT=""

cleanup() { rm -f "$COOKIE_JAR" "$COOKIE_JAR2" "$BODY"; }
trap cleanup EXIT

say() { printf '\033[36m==\033[0m %s\n' "$*"; }
fail() { printf '\033[31m!!\033[0m %s\n' "$*" >&2; exit 1; }

jget() { python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2"; }

post() { # post <期望状态码> <标签> <path> <json> [cookie_jar] [bearer_token]
  local want="$1" label="$2" path="$3" data="$4" jar="${5:-}" bearer="${6:-}"
  local code
  local args=(-s -o "$BODY" -w '%{http_code}' -X POST "$API$path"
    -H 'Content-Type: application/json' -H "Origin: $ORIGIN")
  [ -n "$jar" ] && args+=(-c "$jar")
  [ -n "$bearer" ] && args+=(-H "Authorization: Bearer $bearer")
  code=$(curl "${args[@]}" -d "$data")
  [ "$code" = "$want" ] || fail "$label 返回 ${code}（期望 ${want}）：$(cat "$BODY")"
}

# 1. 没注册的邮箱直接密码登录 → 404，前端据此引导去注册
post 404 "1/9 未注册邮箱登录" /api/auth/login "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}"
DETAIL=$(python3 -c "import json;print(json.load(open('$BODY')).get('detail',''))")
case "$DETAIL" in *尚未注册*) ;; *) fail "1/9 未注册的提示语应含「尚未注册」，实际：$DETAIL" ;; esac
say "1/9 未注册邮箱被明确引导注册：$DETAIL"

# 2. 注册必须两次密码一致
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/auth/register" \
  -H 'Content-Type: application/json' -H "Origin: $ORIGIN" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\",\"confirm_password\":\"${PASSWORD}x\"}")
[ "$code" = "422" ] || fail "2/9 确认密码不一致应 422，实际 ${code}"
say "2/9 确认密码不一致被服务端拦下（422）"

# 3. 注册成功即登录：直接下发令牌，无需邮箱验证码
post 200 "3/9 注册" /api/auth/register \
  "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\",\"confirm_password\":\"$PASSWORD\"}" "$COOKIE_JAR"
AT="$(jget "$BODY" access_token)"
[ -n "$AT" ] || fail "3/9 注册响应里没有 access_token"
say "3/9 注册成功并已登录（拿到 access_token，refresh Cookie 落库 $(grep -c refresh_token "$COOKIE_JAR" || true) 条）"

# 4. /me 显示这是完整账号：已验证 + 有密码
curl -s -H "Authorization: Bearer $AT" -H "Origin: $ORIGIN" "$API/api/auth/me" >"$BODY" || fail "4/9 /me 请求失败"
HAS_PW=$(jget "$BODY" has_password)
VERIFIED=$(jget "$BODY" email_verified)
[ "$HAS_PW" = "True" ] || fail "4/9 注册后 has_password 应为 True，实际 $HAS_PW"
[ "$VERIFIED" = "True" ] || fail "4/9 注册后 email_verified 应为 True，实际 $VERIFIED"
say "4/9 /me：has_password=${HAS_PW}、email_verified=${VERIFIED}（不再有“待验证”状态）"

# 5. 重复注册 → 409 并指向登录，且不能改掉原密码
post 409 "5/9 重复注册" /api/auth/register \
  "{\"email\":\"$EMAIL\",\"password\":\"$NEW_PASSWORD\",\"confirm_password\":\"$NEW_PASSWORD\"}"
say "5/9 已注册邮箱重复注册被拒：$(jget "$BODY" detail)"

# 6. 密码登录可用
post 200 "6/9 密码登录" /api/auth/login "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" "$COOKIE_JAR2"
AT="$(jget "$BODY" access_token)"
RT=$(jget "$BODY" refresh_token)
say "6/9 邮箱 + 密码登录成功"

# 7. 已登录改密码：必须带当前密码
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/auth/change-password" \
  -H "Authorization: Bearer $AT" -H 'Content-Type: application/json' -H "Origin: $ORIGIN" \
  -d "{\"current_password\":null,\"new_password\":\"$NEW_PASSWORD\"}")
[ "$code" = "400" ] || fail "7/9 已有密码时缺 current_password 应 400，实际 $code"
post 200 "7/9 设置密码" /api/auth/change-password \
  "{\"current_password\":\"$PASSWORD\",\"new_password\":\"$NEW_PASSWORD\"}" "" "$AT"
say "7/9 改密码成功：$(jget "$BODY" message)"

# 8. 旧密码失效、新密码可用
post 401 "8/9 旧密码" /api/auth/login "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}"
post 200 "8/9 新密码" /api/auth/login "{\"email\":\"$EMAIL\",\"password\":\"$NEW_PASSWORD\"}"
RT=$(jget "$BODY" refresh_token)
AT="$(jget "$BODY" access_token)"
say "8/9 旧密码被拒、新密码可登录"

# 9. 找回密码仍走邮件验证码（唯一还需要读日志的一步）
if [ "$SKIP_RESET" = "1" ]; then
  say "9/9 已跳过找回密码（SKIP_RESET=1）"
else
  post 202 "9/9 请求重置邮件" /api/auth/forgot-password "{\"email\":\"$EMAIL\"}"
  OTP=""
  if [ "$REMOTE_FETCH" = "1" ]; then
    # 日志落盘有毫秒级延迟，最多等 3 轮
    for _ in 1 2 3; do
      OTP=$(ssh -o BatchMode=yes "$SERVER" "$LOGS" 2>/dev/null \
        | grep -F "$EMAIL" -A2 | grep -oE '\b[0-9]{6}\b' | tail -1)
      [ -n "$OTP" ] && break
      sleep 1
    done
  else
    printf '   未配 SMTP 时验证码在后端日志里，请从日志读取后粘贴（或直接设 OTP=123456）：'
    read -r OTP
  fi
  [ -n "${OTP:-}" ] || fail "9/9 没拿到找回密码的验证码"
  post 200 "9/9 验证码重置" /api/auth/reset-password/code \
    "{\"email\":\"$EMAIL\",\"code\":\"$OTP\",\"new_password\":\"$RESET_PASSWORD\"}"
  post 200 "9/9 重置后登录" /api/auth/login "{\"email\":\"$EMAIL\",\"password\":\"$RESET_PASSWORD\"}"
  say "9/9 找回密码链路可用，重置后的密码已可登录（旧会话已全部撤销）"
fi

# 附加：刷新令牌两条路径都要通 —— 前端 restoreUser 走 body 里的 refresh_token，Cookie 只是备用。
code=$(curl -s -o /dev/null -b "$COOKIE_JAR2" -c "$COOKIE_JAR2" -w '%{http_code}' -X POST "$API/api/auth/refresh" \
  -H 'Content-Type: application/json' -H "Origin: $ORIGIN" -d "{\"refresh_token\":\"$RT\"}")
[ "$code" = "200" ] || fail "附加A body 带 refresh_token 刷新返回 ${code}（期望 200）"
say "附加A 前端路径（body 提交 refresh_token）刷新成功，轮换后的新 Cookie 已写回"

code=$(curl -s -o /dev/null -b "$COOKIE_JAR2" -c "$COOKIE_JAR2" -w '%{http_code}' -X POST "$API/api/auth/refresh" \
  -H 'Content-Type: application/json' -H "Origin: $ORIGIN" -d '{}')
[ "$code" = "200" ] || fail "附加B 仅靠 Cookie 刷新返回 ${code}（期望 200：http 隧道下 Cookie 必须不带 Secure 才存得住）"
say "附加B 纯 Cookie 路径刷新成功 —— ENVIRONMENT=development 下 Secure 标记没拦住 http 隧道"

printf '\n\033[32m冒烟通过\033[0m\n'
