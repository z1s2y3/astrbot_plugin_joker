#!/usr/bin/env bash
set -euo pipefail

PLUGIN_DIR="${1:-/www/wwwroot/joker-web}"
PYTHON_BIN="${2:-python3}"

if [[ ! -f "$PLUGIN_DIR/web_client/requirements.txt" ]]; then
  echo "错误：未找到 $PLUGIN_DIR/web_client/requirements.txt"
  exit 1
fi

cd "$PLUGIN_DIR"
if [[ -d .web-venv ]] && ! .web-venv/bin/python -m pip --version >/dev/null 2>&1; then
  echo "检测到不完整的 .web-venv（缺少 pip），将自动重建。"
  rm -rf .web-venv
fi
if [[ ! -d .web-venv ]]; then
  "$PYTHON_BIN" -m venv .web-venv
fi
.web-venv/bin/python -m pip install --upgrade pip
.web-venv/bin/python -m pip install -r web_client/requirements.txt

if [[ ! -f web_client/.env ]]; then
  cp web_client/.env.example web_client/.env
  chmod 600 web_client/.env
  echo "已创建 web_client/.env，请修改其中所有 replace_with_ 项。"
else
  echo "已保留现有 web_client/.env，未覆盖密码和密钥。"
fi

.web-venv/bin/python -m compileall -q web_client
echo "网页依赖和代码检查完成。"
