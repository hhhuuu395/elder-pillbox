#!/usr/bin/env bash
# 老人智能药盒 · 后端链路自检
#
# 用法（先起服务，另开一个终端跑）：
#     bash curl_checks.sh
#
# 为什么请求体都写成 heredoc 而不是 curl -d '{"name":"阿莫西林"}'：
#   Windows 的 Git Bash 会把命令行参数里的非 ASCII 字符转到 ANSI 码页（GBK），
#   服务端按 UTF-8 解析就会报 "There was an error parsing the body"。
#   走 stdin（heredoc + --data-binary @-）不经过 argv，字节原样送达。
#
# 覆盖范围：
#   主链路  mock 拍照 -> AI 解析 -> 子女确认 -> 今日药历 -> 打卡
#   异常    AI 返回缺字段 / 校验器规则 1-6 / 确认被拦 / 超时三选一

set -u
BASE="${BASE:-http://127.0.0.1:8000}"
PY="${PY:-./.venv/Scripts/python.exe}"
export PYTHONIOENCODING=utf-8

pass=0; fail=0

jget()  { "$PY" -c "import json,sys;d=json.load(sys.stdin);print($1)"; }
jcount(){ "$PY" -c "import json,sys;d=json.load(sys.stdin);print(sum(1 for i in d['issues'] if i['field']=='$1' and i['code']=='$2'))"; }
step()  { printf '\n\033[1m──── %s ────\033[0m\n' "$*"; }
check() {
  if [ "$2" = "$3" ]; then printf '  \033[32m[PASS]\033[0m %s\n' "$1"; pass=$((pass+1))
  else printf '  \033[31m[FAIL]\033[0m %s  (expected=%s actual=%s)\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}

# ── 0. 复位 ─────────────────────────────────────────────────────────────────
step "0. 复位 + 健康检查"
curl -s -X POST "$BASE/api/dev/reset" >/dev/null
curl -s -X POST "$BASE/api/dev/parse_mode" -H 'Content-Type: application/json' \
     -d '{"mode":"missing_fields"}' >/dev/null
HEALTH=$(curl -s "$BASE/api/health")
check "服务在线" "True" "$(echo "$HEALTH" | jget 'd["ok"]')"
echo "  提示：api_key_configured = $(echo "$HEALTH" | jget 'd["api_key_configured"]')" \
     "（false 时 parse 会返回 no_api_key，但下面的 dev 模式仍可跑通全链路）"

# ── 1. mock 拍照 ────────────────────────────────────────────────────────────
step "1. Mock 拍照 OCR"
SCAN=$(curl -s -X POST "$BASE/api/ocr/scan")
check "返回固定药盒文字" "mock" "$(echo "$SCAN" | jget 'd["source"]')"
echo "$SCAN" | jget 'd["raw_text"]' | sed 's/^/    /'

# ── 2. 异常路径：AI 返回缺字段 ──────────────────────────────────────────────
step "2. 异常路径：AI 返回缺字段 -> 不生成计划，全部走 issues 通道"
PLAN=$(curl -s -X POST "$BASE/api/parse" -H 'Content-Type: application/json' -d '{}')
PID=$(echo "$PLAN" | jget 'd["plan_id"]')
check "plan 停在 draft"          "draft" "$(echo "$PLAN" | jget 'd["status"]')"
check "blocking 为真"            "True"  "$(echo "$PLAN" | jget 'd["blocking"]')"
check "规则2 剂量缺失被拦"       "1"     "$(echo "$PLAN" | jcount dose_amount missing)"
check "规则3 遵医嘱不猜频次"     "1"     "$(echo "$PLAN" | jcount times_per_day missing)"
check "规则5 疗程缺失只提示"     "2"     "$(echo "$PLAN" | jcount duration_days soft_missing)"
check "规则6 疑似编造药名告警"   "1"     "$(echo "$PLAN" | jcount name fabricated)"

step "2b. 异常路径：AI 返回的不是合法 json -> 重试后仍失败，同样走 issues"
curl -s -X POST "$BASE/api/dev/parse_mode" -H 'Content-Type: application/json' \
     -d '{"mode":"malformed"}' >/dev/null
BAD=$(curl -s -X POST "$BASE/api/parse" -H 'Content-Type: application/json' -d '{}')
check "plan 停在 draft"   "draft"    "$(echo "$BAD" | jget 'd["status"]')"
check "blocking 为真"     "True"     "$(echo "$BAD" | jget 'd["blocking"]')"
check "标记为 ai_malformed" "ai_malformed" \
  "$(echo "$BAD" | jget '[i["code"] for i in d["issues"] if i["code"]=="ai_malformed"][0]')"
check "解析不出药 -> 提示手动录入" "empty" \
  "$(echo "$BAD" | jget '[i["code"] for i in d["issues"] if i["code"]=="empty"][0]')"
curl -s -X POST "$BASE/api/dev/parse_mode" -H 'Content-Type: application/json' \
     -d '{"mode":"missing_fields"}' >/dev/null

# ── 3. 异常路径：确认被拦 ───────────────────────────────────────────────────
step "3. 异常路径：缺口没补完就点确认 -> 400，不生成计划"
CODE=$(curl -s -o /tmp/reject.json -w '%{http_code}' -X POST "$BASE/api/plans/$PID/confirm" \
  -H 'Content-Type: application/json' --data-binary @- <<'JSON'
{"items":[{"name":"阿莫西林胶囊","dose_amount":null,"dose_unit":"g","times_per_day":3,"frequency_raw":"一日三次","duration_days":7,"duration_raw":"疗程7天"}]}
JSON
)
check "HTTP 400" "400" "$CODE"
check "确认被拒绝" "True" "$(cat /tmp/reject.json | jget 'd["blocking"]')"

step "3b. 异常路径：规则1 药名为空 + 规则4 频次越界"
CODE=$(curl -s -o /tmp/reject2.json -w '%{http_code}' -X POST "$BASE/api/plans/$PID/confirm" \
  -H 'Content-Type: application/json' -d '{"items":[{"name":"","dose_amount":1,"dose_unit":"pcs","times_per_day":9,"duration_days":3}]}')
check "HTTP 400" "400" "$CODE"
check "规则1 药名缺失被拦" "1" "$(cat /tmp/reject2.json | jcount name missing)"
check "规则4 频次越界被拦" "1" "$(cat /tmp/reject2.json | jcount times_per_day out_of_range)"

step "3c. 数值清洗：nan / inf / 超大整数 -> 一律置 null，绝不能是 500"
# starlette 用 json.dumps(allow_nan=False)，放 nan/inf 进去会抛 ValueError 变 500。
# 正确行为是降级为 null -> 校验器当成"剂量缺失" -> 400 + blocking。
BIGINT=$("$PY" -c "print('9'*400)")
for BADVAL in '"nan"' '"Infinity"' '"-inf"' '"1e999"' "$BIGINT"; do
  CODE=$(curl -s -o /tmp/badnum.json -w '%{http_code}' -X POST "$BASE/api/plans/$PID/confirm" \
    -H 'Content-Type: application/json' \
    -d "{\"items\":[{\"name\":\"X\",\"dose_amount\":$BADVAL,\"dose_unit\":\"mg\",\"times_per_day\":1,\"duration_days\":3}]}")
  check "dose_amount=${BADVAL:0:12} -> 400 而非 500" "400" "$CODE"
done
check "脏数值被当成剂量缺失处理" "1" "$(cat /tmp/badnum.json | jcount dose_amount missing)"

# ── 4. 主链路：补全后确认生效 ───────────────────────────────────────────────
step "4. 主链路：子女补全两处缺口 -> 确认生效 -> 展开今日 Dose"
OK=$(curl -s -X POST "$BASE/api/plans/$PID/confirm" \
  -H 'Content-Type: application/json' --data-binary @- <<'JSON'
{"items":[
 {"name":"阿莫西林胶囊","dose_amount":0.25,"dose_unit":"g","times_per_day":3,"frequency_raw":"一日三次","duration_days":7,"duration_raw":"疗程7天"},
 {"name":"硝苯地平控释片","dose_amount":30,"dose_unit":"mg","times_per_day":1,"frequency_raw":"一日一次","duration_days":null,"duration_raw":""},
 {"name":"布洛芬缓释胶囊","dose_amount":1,"dose_unit":"粒","times_per_day":2,"frequency_raw":"遵医嘱","duration_days":null,"duration_raw":""}
]}
JSON
)
check "plan 转 active" "active" "$(echo "$OK" | jget 'd["status"]')"
check "软提示不阻塞确认" "False" "$(echo "$OK" | jget 'd["blocking"]')"

# ── 5. 老人端今日药历 ───────────────────────────────────────────────────────
step "5. 老人端今日药历（后端已分好组 + 标好当前时段）"
curl -s "$BASE/api/doses/today" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print("  今天 %s  现在 %s  当前时段 [%s]  宽限 %d 分钟" %
      (d["today"], d["now"][11:], d["current_slot_label"], d["grace_minutes"]))
for g in d["slots"]:
    if not g["doses"]: continue
    print("  %s %s%s" % ("▶" if g["is_current"] else " ", g["label"], "  <<< 当前时段" if g["is_current"] else ""))
    for x in g["doses"]:
        print("      %-16s %-8s %s" % (x["name"], x["dose_text"], x["status_label"]))
print("  待处理超时: %d 条" % len(d["overdue_doses"]))
'

# ── 6. 打卡 ─────────────────────────────────────────────────────────────────
step "6. 打卡：挑一条 pending 的记时间"
DOSE=$(curl -s "$BASE/api/doses/today" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
for g in d["slots"]:
    for x in g["doses"]:
        if x["status"]=="pending": print(x["id"]); raise SystemExit
print("")')
if [ -n "$DOSE" ]; then
  check "打卡后状态=已服用" "taken" "$(curl -s -X POST "$BASE/api/doses/$DOSE/take" | jget 'd["status"]')"
  check "重复打卡被拒(409)" "409" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/doses/$DOSE/take")"
else
  echo "  (当前时段没有 pending 的药，跳过——先用 /api/dev/reset 复位，或调 advance_time 回拨)"
fi

# ── 7. 超时三选一 ───────────────────────────────────────────────────────────
step "7. 超时未服：虚拟时钟拨快 -> overdue -> 三选一"
# 锚到"今天 20:00"而不是固定 +720 分钟。
# 固定 +720 会跨过午夜：/api/doses/today 跟着翻到第二天，而第二天的时段全在未来，
# 一条 overdue 都没有——这个脚本就会在中午之后必挂（下午才跑得出来，上午跑不出来）。
# 拨到 20:00 是同时段的确定值：早/中/晚都过点，睡前(21:30)还没到。
NOW_ISO=$(curl -s "$BASE/api/doses/today" | jget 'd["now"]')
DELTA=$(NOW_ISO="$NOW_ISO" "$PY" -c "
import os, datetime
now = datetime.datetime.fromisoformat(os.environ['NOW_ISO'])
print(int((now.replace(hour=20, minute=0, second=0, microsecond=0) - now).total_seconds() // 60))
")
curl -s -X POST "$BASE/api/dev/advance_time" -H 'Content-Type: application/json' \
     -d "{\"minutes\":$DELTA}" | jget '"  虚拟时钟 -> " + d["now"]'
TODAY=$(curl -s "$BASE/api/doses/today")
check "出现 overdue" "True" "$(echo "$TODAY" | jget 'len(d["overdue_doses"]) > 0')"

OD=$(echo "$TODAY" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);print(d["overdue_doses"][0]["id"] if d["overdue_doses"] else "")')
check "已带出 -> 挂起" "suspended" \
  "$(curl -s -X POST "$BASE/api/doses/$OD/resolve" -H 'Content-Type: application/json' \
      -d '{"action":"carried_out"}' | jget 'd["status"]')"
check "已带出不产生漏服通知" "0" "$(curl -s "$BASE/api/notifications" | jget 'd["count"]')"

OD2=$(curl -s "$BASE/api/doses/today" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);print(d["overdue_doses"][0]["id"] if d["overdue_doses"] else "")')
if [ -n "$OD2" ]; then
  check "确实漏服 -> missed" "missed" \
    "$(curl -s -X POST "$BASE/api/doses/$OD2/resolve" -H 'Content-Type: application/json' \
        -d '{"action":"missed"}' | jget 'd["status"]')"
  check "漏服通知子女 +1" "1" "$(curl -s "$BASE/api/notifications" | jget 'd["count"]')"
  curl -s "$BASE/api/notifications" | jget '"  通知内容: " + d["items"][0]["text"]'
else
  echo "  (没有第二条 overdue 了，跳过漏服分支)"
fi

# ── 收尾 ────────────────────────────────────────────────────────────────────
step "结果"
printf '  PASS %d   FAIL %d\n' "$pass" "$fail"
[ "$fail" -eq 0 ] && echo "  全部通过。" || echo "  有失败项，见上面 [FAIL]。"
