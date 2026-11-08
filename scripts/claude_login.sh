#!/bin/zsh
# 给远窗用的 Claude Code 登录一次（浏览器里点授权即可）。
cd "$(dirname "$0")/.."
CLI=$(.venv/bin/python -c "import claude_agent_sdk,os;print(os.path.join(os.path.dirname(claude_agent_sdk.__file__),'_bundled','claude'))")
"$CLI" auth login
"$CLI" auth status
