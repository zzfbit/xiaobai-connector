#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
source_repo="$project_dir/../voice"
if [[ -n "${VOICE_REPO:-}" ]]; then
  source_repo="$VOICE_REPO"
fi
allow_dirty=0
build_package=1

usage() {
  cat <<'EOF'
用法：
  ./packaging/sync-from-voice.sh [选项]

选项：
  --source PATH       指定 voice 仓库；默认是 ../voice
  --allow-dirty       允许目标仓库有未提交改动（仅适合本地开发）
  --no-build          只做同步检查和测试，不重打安装包
  -h, --help          显示帮助

说明：voice/integrations/connector 与本仓库是两套不同的运行时结构，
不能整目录覆盖。本脚本会检查已移植的协议契约、拦截尚未移植的运行时代码，
然后运行测试并构建 macOS 安装包。
EOF
}

fail() {
  echo "同步失败：$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --source)
      (($# >= 2)) || fail "--source 缺少路径"
      source_repo="$2"
      shift 2
      ;;
    --allow-dirty)
      allow_dirty=1
      shift
      ;;
    --no-build)
      build_package=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "未知参数：$1"
      ;;
  esac
done

source_repo="$(cd "$source_repo" 2>/dev/null && pwd)" \
  || fail "找不到 voice 仓库：$source_repo"
[[ -d "$source_repo/.git" ]] || fail "不是 Git 仓库：$source_repo"
[[ -d "$source_repo/integrations/connector" ]] \
  || fail "voice 仓库缺少 integrations/connector：$source_repo"

if ((allow_dirty == 0)); then
  if ! git -C "$project_dir" diff --quiet || \
     ! git -C "$project_dir" diff --cached --quiet; then
    fail "目标仓库有未提交改动；确认不会覆盖后再用 --allow-dirty"
  fi
fi

source_dirty="$(git -C "$source_repo" status --short --untracked-files=no \
  -- integrations/connector)"
[[ -z "$source_dirty" ]] \
  || fail "voice 的 integrations/connector 有未提交改动，请先提交：$source_dirty"

source_head="$(git -C "$source_repo" log -1 --format=%H -- integrations/connector)"
[[ -n "$source_head" ]] || fail "无法确定 voice Connector 的最新提交"
lock_file="$project_dir/packaging/voice-sync.lock"
base_commit="$(awk '!/^#/ && NF {print $1; exit}' "$lock_file" 2>/dev/null || true)"
[[ "$base_commit" =~ ^[0-9a-f]{40}$ ]] || fail "同步基线无效：$lock_file"
git -C "$source_repo" cat-file -e "$base_commit^{commit}" \
  || fail "同步基线不在 voice 仓库历史中：$base_commit"

if [[ "$source_head" != "$base_commit" ]]; then
  git -C "$source_repo" merge-base --is-ancestor "$base_commit" "$source_head" \
    || fail "voice 历史发生分叉，请先人工核对 Connector 变更"
  changed_files="$(git -C "$source_repo" diff --name-only "$base_commit..$source_head" \
    -- integrations/connector)"
  unsupported=""
  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    case "$path" in
      integrations/connector/visibility.py)
        # This source-only predicate is not part of the standalone runtime.
        ;;
      *)
        unsupported="$unsupported$path\n"
        ;;
    esac
  done <<< "$changed_files"
  [[ -z "$unsupported" ]] \
    || fail "voice 有尚未移植的运行时代码变更：\n$unsupported"
  echo "voice 只有独立版不消费的可见性辅助文件变更，继续构建。"
fi

source_client="$source_repo/integrations/connector/client.py"
source_spool="$source_repo/integrations/connector/spool.py"
target_gateway="$project_dir/src/xiaobai_connector/gateway.py"
target_spool="$project_dir/src/xiaobai_connector/spool.py"
for event in codex.sequence.create codex.sequence.item codex.sequence.start codex.sequence.cancel; do
  grep -Fq "\"$event\"" "$source_client" \
    || fail "voice Connector 缺少顺序任务事件：$event"
  grep -Fq "\"$event\"" "$target_gateway" \
    || fail "独立 Connector 尚未支持顺序任务事件：$event"
done
grep -Fq "task_sequences" "$source_spool" \
  || fail "voice Connector 缺少顺序任务持久化表"
grep -Fq "task_sequences" "$target_spool" \
  || fail "独立 Connector 缺少顺序任务持久化表"

python_bin="$project_dir/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  python_bin="$(command -v python3.11 || command -v python3 || true)"
fi
[[ -n "$python_bin" ]] || fail "找不到 Python 3.11+"

echo "运行独立 Connector 测试：$python_bin"
"$python_bin" -m pytest -q "$project_dir/tests"

if ((build_package)); then
  [[ "$(uname -s)" == "Darwin" ]] \
    || fail "当前不是 macOS；请在 Windows 使用 packaging/build-windows.ps1"
  echo "构建 macOS 安装包。"
  PYTHON_BIN="$python_bin" "$project_dir/packaging/build-macos.sh"
  echo "已更新："
  stat -f '  %N (%z bytes, %Sm)' -t '%Y-%m-%d %H:%M:%S' \
    "$project_dir/dist/Xiaobai Connector.app" \
    "$project_dir/dist/Xiaobai-Connector-macos.dmg"
fi

version="$("$python_bin" -c 'import xiaobai_connector; print(xiaobai_connector.__version__)')"
printf '同步检查完成：voice=%s，独立版=%s。\n' "$source_head" "$version"
