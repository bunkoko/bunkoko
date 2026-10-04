#!/bin/bash
# Mac の初回準備（何度実行してもよい。済んでいる所は飛ばす）。
# Python 本体とパッケージはこのフォルダの中（.python と .venv）だけに入れ、Mac 全体には入れない。
# 不要になったら bunkoko フォルダごと消せば元どおり（uv だけは Homebrew で入る）。
set -euo pipefail
cd "$(dirname "$0")/.."
PY_VERSION="${CFD_PYTHON:-3.12}"
export UV_PYTHON_INSTALL_DIR="$PWD/.python"   # uv が落とす Python 本体の置き場（このフォルダの中）
export UV_PYTHON_BIN_DIR="$PWD/.python/bin"     # python3.12 のリンクも ~/.local/bin ではなくここに置く

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

step "2/5 Python ${PY_VERSION} と仮想環境 .venv"
if [ -x .venv/bin/python ] && .venv/bin/python -c "import sys; sys.exit(0 if '%d.%d' % sys.version_info[:2] == '${PY_VERSION}' else 1)" 2>/dev/null; then
  echo "作成済み: $(.venv/bin/python --version)"
else
  # Mac に元からある Python は探さない（古い Intel 用の python3 などがあると uv が止まるため）。
  # uv が配布している Python をこのフォルダに入れ、その場所を直接指定する
  uv python install --no-bin "${PY_VERSION}"
  PY_BIN="$(ls -d "${UV_PYTHON_INSTALL_DIR}"/cpython-"${PY_VERSION}"*/bin/python"${PY_VERSION}" 2>/dev/null | sort | tail -1 || true)"
  if [ -z "${PY_BIN}" ] || [ ! -x "${PY_BIN}" ]; then
    echo "Python ${PY_VERSION} が ${UV_PYTHON_INSTALL_DIR} に見つからない。表示をそのまま送ってください"
    ls -la "${UV_PYTHON_INSTALL_DIR}" || true
    exit 1
  fi
  rm -rf .venv
  uv venv --python "${PY_BIN}" .venv
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
