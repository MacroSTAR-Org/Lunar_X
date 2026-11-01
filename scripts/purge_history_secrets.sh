#!/usr/bin/env bash
# ============================================================================
# 清理 git 历史中的敏感信息
#
# 背景：早期版本把 config.json 提交进了仓库，历史里留下了真实的
# DeepSeek / Gemini API key 与个人 QQ 号。虽然当前工作树已经干净，
# 但 clone 过仓库的人依然能从旧提交里翻出这些值。
#
# 本脚本做两件事：
#   1. 从所有历史提交中彻底移除 config.json（改为只保留一个示例文件）
#   2. 清掉 examples/appsettings.json 里的真实 Uin / AccessToken / 签名服务器
#
# 用法：
#   bash scripts/purge_history_secrets.sh          # 演练：只打印将要执行的命令
#   bash scripts/purge_history_secrets.sh --apply  # 真正执行（会重写历史）
#
# ⚠️ 重写历史会改变所有 commit 的 SHA。执行前务必确认：
#   - 已通知所有协作者（他们需要重新 clone 或 fetch + reset --hard）
#   - 已确认远程分支没有他人未推送的提交
#   - 已做好备份（脚本会自动在 .git/ 下留一份 bundle）
#
# 无论历史是否清理，**泄漏过的 API key 都必须到服务商后台吊销并重新生成**。
# 清理历史不能替代轮换密钥。
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

if ! command -v git-filter-repo >/dev/null 2>&1; then
    echo "未找到 git-filter-repo，安装方式："
    echo "  uv tool install git-filter-repo"
    echo "  或： pipx install git-filter-repo / pip install git-filter-repo"
    exit 1
fi

echo "==> 仓库: $REPO_ROOT"
echo "==> 当前 HEAD: $(git rev-parse --short HEAD)"
echo

if [[ $APPLY -eq 0 ]]; then
    cat <<'EOF'
[演练模式] 将会执行：

  1. 备份整个仓库到 .git/lunarx-backup-<时间戳>.bundle
  2. git filter-repo --force \
       --path config.json --invert-paths \
       --path examples/appsettings.json \
       --replace-text <替换规则>
     即：从历史中删除 config.json，并把 appsettings.json 的
         Uin / AccessToken / SignServerUrl 替换为占位符
  3. 清理 reflog 与 origin 引用（否则旧对象仍可通过 SHA 访问）
  4. 提示你 force push 并轮换密钥

确认无误后加 --apply 执行。
EOF
    exit 0
fi

# ---------------------------------------------------------------- 备份
BACKUP=".git/lunarx-backup-$(date +%Y%m%d-%H%M%S).bundle"
echo "==> [1/4] 备份仓库到 $BACKUP"
git bundle create "$BACKUP" --all
echo "    备份完成，回滚方式: git clone $BACKUP ."
echo

# ---------------------------------------------------------------- 替换规则
RULES="$(mktemp)"
cat >"$RULES" <<'EOF'
literal:179495819==>0
literal:114514==>PLEASE_CHANGE_ME
literal:http://39.108.115.52:58080/api/sign/39038==>
EOF
echo "==> [2/4] 重写历史"
git filter-repo --force \
    --path config.json --invert-paths \
    --path examples/appsettings.json \
    --replace-text "$RULES"
rm -f "$RULES"
echo

# ---------------------------------------------------------------- 清理残留
echo "==> [3/4] 清理 reflog 与旧引用"
git reflog expire --expire=now --all
git gc --prune=now --aggressive
echo

echo "==> [4/4] 完成"
cat <<'EOF'

后续必做（缺一不可）：

  1. 强制推送（团队成员需重新 clone）：
       git push --force --mirror origin

  2. 通知协作者：旧 commit SHA 全部失效，请重新 clone 或
       git fetch origin && git reset --hard origin/<branch>

  3. 到服务商后台吊销并重新生成泄漏过的密钥：
       - DeepSeek  API Key
       - Gemini   API Key
     清理历史只让旧值失效不了，必须显式吊销。

  4. 检查 forks / PR / CI 缓存里是否还留有旧对象，必要时联系平台清理。
EOF
