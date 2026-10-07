#!/bin/bash
# check-regressions.sh — 同步后回归对账：已知上游债自动豁免，只报新增失败
# 用法:
#   ./tasks/check-regressions.sh                  # 全量: tests/server tests/misc tests/client tests/session
#   ./tasks/check-regressions.sh tests/misc       # 只跑指定目录/文件
#   ./tasks/check-regressions.sh --offline FILE   # 不跑测试, 对已有 FAILED 清单文件对账
# 退出码: 0=无新增失败(零回归) 1=有新增失败(人工排查) 2=用法错误
set -euo pipefail
DEBT_FILE="$(cd "$(dirname "$0")" && pwd)/upstream-test-debt.txt"
KNOWN=$(grep -v '^#' "$DEBT_FILE" | sort -u)

mode="run"
if [ "${1:-}" = "--offline" ]; then
  mode="offline"
  shift
fi

extract_failed() {
  # pytest 输出 → 规范化 test id 集合（去掉 FAILED 前缀、说明尾巴、worktree 路径前缀）
  grep FAILED | sed 's/FAILED //;s/ - .*//;s|/tmp/ov-[a-z-]*/||g' | sort -u
}

if [ "$mode" = "offline" ]; then
  [ -n "${1:-}" ] || { echo "usage: $0 --offline <FAILED清单文件>" >&2; exit 2; }
  CURRENT=$(grep -v '^#' "$1" | sort -u)
else
  DIRS=("$@")
  [ ${#DIRS[@]} -eq 0 ] && DIRS=(tests/server tests/misc tests/client tests/session)
  VENV_ACT="$(pwd)/.venv/bin/activate"
  if [ -f "$VENV_ACT" ]; then
    # shellcheck disable=SC1090
    source "$VENV_ACT"
  fi
  OUT=$(mktemp)
  trap 'rm -f "$OUT"' EXIT
  echo ">> pytest ${DIRS[*]}"
  echo ">> 对账基线: $DEBT_FILE"
  python -m pytest "${DIRS[@]}" -q --no-cov --tb=no 2>/dev/null | extract_failed > "$OUT" || true
  CURRENT=$(cat "$OUT")
fi

NEW=$(comm -23 <(echo "$CURRENT") <(echo "$KNOWN"))
GONE=$(comm -13 <(echo "$CURRENT") <(echo "$KNOWN"))
if [ "$mode" = "run" ]; then
  # 只提示本次实际跑过的目录范围内的未复现债, 避免单目录跑时全量清单刷屏
  PAT=$(IFS='|'; echo "${DIRS[*]}")
  GONE=$(echo "$GONE" | grep -E "^($PAT)" || true)
fi

echo ">> 当前失败: $(echo "$CURRENT" | grep -c . || true)  已知债: $(echo "$KNOWN" | grep -c . || true)"

if [ -z "$NEW" ]; then
  echo ">> ✅ 零新增失败 — 已知债全部豁免, 无合并回归"
  if [ -n "$GONE" ]; then
    echo ">> ℹ️  以下已知债本轮未复现 (可能已被修复或测试改名, 建议人工确认后清理清单):"
    echo "$GONE"
  fi
  exit 0
else
  echo ">> ❌ 新增失败 $(echo "$NEW" | grep -c .) 条 (疑似合并引入, 需人工归因):"
  echo "$NEW"
  exit 1
fi
