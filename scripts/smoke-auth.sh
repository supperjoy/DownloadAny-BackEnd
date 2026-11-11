#!/usr/bin/env bash
# 认证链路冒烟测试：验证「验证码即可登录（无需密码）→ 用户中心设置密码 → 密码登录」这条产品流程。
#
#   bash scripts/smoke-auth.sh                       # 默认打 http://127.0.0.1:8000（配 SSH 隧道）
#   API=http://127.0.0.1:8001 bash scripts/smoke-auth.sh
#   REMOTE_FETCH=0 bash scripts/smoke-auth.sh        # 本机跑后端：改为手工粘贴日志里的验证码
#
# 默认通过 ssh 到 SERVER 的容器日志里取验证码（未配 SMTP 的联调形态），命令本身不用带参数。
#
# 只在开发/联调环境跑：验证码从日志里取，账号是一次性邮箱，任何密钥/令牌都不打印。
set -uo pipefail

API="${API:-http://127.0.0.1:8000}"
ORIGIN="${ORIGIN:-http://localhost:1420}"
EMAIL="${EMAIL:-smoke-$(date +%s)@example.com}"
NEW_PASSWORD="${NEW_PASSWORD:-Sm0ke$(date +%s)-pad}"
SERVER="${SERVER:-root@101.96.199.191}"
LOGS="${LOGS:-docker compose -f /opt/downloadany-backend/docker-compose.server.yml logs --since=90s backend}"
# 默认经 ssh 从服务器容器日志取验证码（未配 SMTP 的联调形态）；本机跑后端时设 REMOTE_FETCH=0 手工粘贴
REMOTE_FETCH="${REMOTE_FETCH:-1}"
COOKIE_JAR="$(mktemp)"
COOKIE_JAR2="$(mktemp)"
BODY="$(mktemp)"
AT=""

cleanup() { rm -f "$COOKIE_JAR" "$COOKIE_JAR2" "$BODY"; }
trap cleanup EXIT

say() { printf '\033[36m==\033[0m %s\n' "$*"; }
fail() { printf '\033[31m!!\033[0m %s\n' "$*" >&2; exit 1; }

jget() { python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2"; }

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/auth/login/code/send" \
  -H 'Content-Type: application/json' -H "Origin: $ORIGIN" -d "{\"email\":\"$EMAIL\"}")
[ "$code" = "202" ] || fail "1/7 发送验证码返回 $code（期望 202）。检查后端是否起来：curl $API/health"
say "1/7 验证码已发送到 ${EMAIL}（该邮箱此前未注册）"

if [ "$REMOTE_FETCH" = "1" ]; then
  # 日志落盘有毫秒级延迟，最多等 3 轮
  OTP=""
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
[ -n "${OTP:-}" ] || fail "2/7 没拿到验证码"
say "2/7 取到 6 位验证码（值不打印）"

code=$(curl -s -c "$COOKIE_JAR" -o "$BODY" -w '%{http_code}' -X POST "$API/api/auth/login/code" \
  -H 'Content-Type: application/json' -H "Origin: $ORIGIN" -d "{\"email\":\"$EMAIL\",\"code\":\"$OTP\"}")
[ "$code" = "200" ] || fail "3/7 验证码登录返回 $code：$(cat "$BODY")"
AT="$(jget "$BODY" access_token)"
[ -n "$AT" ] || fail "3/7 响应里没有 access_token"
REFRESH_COUNT=$(grep -c refresh_token "$COOKIE_JAR" || true)
say "3/7 验证码登录成功，未设任何密码；refresh Cookie 落库 ${REFRESH_COUNT} 条"

curl -s -H "Authorization: Bearer $AT" -H "Origin: $ORIGIN" "$API/api/auth/me" >"$BODY" \
  || fail "4/7 /me 请求失败"
HAS_PW=$(jget "$BODY" has_password)
VERIFIED=$(jget "$BODY" email_verified)
[ "$HAS_PW" = "False" ] || fail "4/7 新账户的 has_password 应为 False，实际 $HAS_PW"
say "4/7 /me：has_password=${HAS_PW}（用户中心会显示「设置密码」）、email_verified=${VERIFIED}"

code=$(curl -s -o "$BODY" -w '%{http_code}' -X POST "$API/api/auth/change-password" \
  -H "Authorization: Bearer $AT" -H 'Content-Type: application/json' -H "Origin: $ORIGIN" \
  -d "{\"current_password\":null,\"new_password\":\"$NEW_PASSWORD\"}")
[ "$code" = "200" ] || fail "5/7 设置密码返回 $code：$(cat "$BODY")"
MSG=$(jget "$BODY" message)
say "5/7 无当前密码也能设置成功（后端应回「密码设置成功」）：${MSG}"

curl -s -H "Authorization: Bearer $AT" -H "Origin: $ORIGIN" "$API/api/auth/me" >"$BODY"
HAS_PW=$(jget "$BODY" has_password)
[ "$HAS_PW" = "True" ] || fail "6/7 设置后 has_password 应变 True，实际 $HAS_PW"
say "6/7 /me：has_password=${HAS_PW}（入口自动变「修改密码」）"

code=$(curl -s -o "$BODY" -c "$COOKIE_JAR2" -w '%{http_code}' -X POST "$API/api/auth/login" \
  -H 'Content-Type: application/json' -H "Origin: $ORIGIN" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$NEW_PASSWORD\"}")
[ "$code" = "200" ] || fail "7/7 用新密码登录返回 $code：$(cat "$BODY")"
RT=$(jget "$BODY" refresh_token)
say "7/7 密码登录可用（同一个账户，从验证码建号到密码登录闭环打通）"

# 刷新令牌两条路径都要通：前端 restoreUser 走 body 里的 refresh_token，
# Cookie 只是备用 —— 之前只发 Cookie 不带 body 会 422（RefreshRequest 是必填 body 参数）。
code=$(curl -s -o /dev/null -b "$COOKIE_JAR2" -c "$COOKIE_JAR2" -w '%{http_code}' -X POST "$API/api/auth/refresh" \
  -H 'Content-Type: application/json' -H "Origin: $ORIGIN" -d "{\"refresh_token\":\"$RT\"}")
[ "$code" = "200" ] || fail "附加A body 带 refresh_token 刷新返回 $code（期望 200）"
say "附加A 前端路径（body 提交 refresh_token）刷新成功，轮换后的新 Cookie 已写回"

code=$(curl -s -o /dev/null -b "$COOKIE_JAR2" -c "$COOKIE_JAR2" -w '%{http_code}' -X POST "$API/api/auth/refresh" \
  -H 'Content-Type: application/json' -H "Origin: $ORIGIN" -d '{}')
[ "$code" = "200" ] || fail "附加B 仅靠 Cookie 刷新返回 $code（期望 200：http 隧道下 Cookie 必须不带 Secure 才存得住）"
say "附加B 纯 Cookie 路径刷新成功 —— ENVIRONMENT=development 下 Secure 标记没拦住 http 隧道"
printf '\n\033[32m冒烟通过\033[0m\n'
