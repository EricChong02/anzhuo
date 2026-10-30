#!/bin/zsh
# 给案头用的 Claude Code 登录一次（浏览器里点授权即可）。如果这台电脑上的 Claude Code 已经登录过，不用再跑。
cd "$(dirname "$0")/.."
CLI=$(.venv/bin/python -c "import claude_agent_sdk,os;print(os.path.join(os.path.dirname(claude_agent_sdk.__file__),'_bundled','claude'))")
"$CLI" auth login
"$CLI" auth status
