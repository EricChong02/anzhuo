#!/bin/sh
# 给受限账号（role = user）准备只读的 Python 工具环境：做课、写教学设计常用的库。
# 受限对话的沙箱只读放行这个目录（见 engines/fence.py 的 TOOL_DIRS），自己不能往里装包。
set -e
DIR="$HOME/.local/share/remote-window-tools"
mkdir -p "$DIR"
uv venv --python 3.13 --allow-existing "$DIR/venv"
uv pip install --python "$DIR/venv/bin/python" python-docx pymupdf pypdf openpyxl numpy pillow
"$DIR/venv/bin/python" -c "import docx, fitz, pypdf, openpyxl, numpy, PIL; print('受限账号工具环境就绪：', '$DIR/venv')"
