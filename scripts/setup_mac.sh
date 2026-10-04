#!/bin/bash
# Mac の初回準備（何度実行してもよい。済んでいる所は飛ばす）。
# Python 本体とパッケージはこのフォルダの中（.python と .venv）だけに入れ、Mac 全体には入れない。
# 不要になったら bunkoko フォルダごと消せば元どおり（uv だけは Homebrew で入る）。
set -euo pipefail
cd "$(dirname "$0")/.."
PY_VERSION="${CFD_PYTHON:-3.12}"
export UV_PYTHON_INSTALL_DIR="$PWD/.python"

step() { printf '\n== %s\n' "$1"; }

step "1/5 uv（Python と仮想環境を用意する道具）"
if ! command -v uv >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    brew install uv
  else
    echo "Homebrew が見つからない。https://brew.sh の手順で入れてから、もう一度実行する"
    exit 1
  fi
fi
uv --version

step "2/5 仮想環境 .venv（Python ${PY_VERSION}）"
if [ -x .venv/bin/python ] && .venv/bin/python -c "import sys; sys.exit(0 if '%d.%d' % sys.version_info[:2] == '${PY_VERSION}' else 1)"; then
  echo "作成済み: $(.venv/bin/python --version)"
else
  rm -rf .venv
  uv venv --python "${PY_VERSION}" .venv
fi

step "3/5 パッケージ（.venv の中に入る）"
uv pip install --python .venv/bin/python -e ".[dev]"

step "4/5 テスト"
.venv/bin/python -m pytest -q

step "5/5 合成データで学習の動作確認（数分〜10分）"
if [ "${CFD_SKIP_DEMO:-0}" = "1" ]; then
  echo "省略（CFD_SKIP_DEMO=1）"
else
  .venv/bin/python scripts/train.py --demo
fi

printf '\n準備完了。以降は ./cfd <コマンド> で実行する（一覧は ./cfd help）\n'
