# 参与贡献

欢迎提 issue 和 PR。这是一个个人维护的小项目，请尽量让改动小而清楚。

## 本地开发

```bash
uv sync                          # 装依赖（Python 3.13+）
.venv/bin/pytest -q              # 跑测试，全部离线
.venv/bin/python scripts/demo.py # 演示模式：不连 AI，看界面
```

- 界面在 `src/anzhuo/static/app.html`（单文件，没有构建步骤），改完刷新浏览器就能看到。
- 改 Python 之后重启 `anzhuo run`；如果案头是用 launchd 常驻的，用 `scripts/install_launchd.sh` 重启。
- 想了解代码怎么分工，读 `docs/架构.md`。

## 提 PR 前

1. `pytest -q` 全部通过；新功能带上测试（`tests/` 里有假钉钉、假 Claude 的写法可参考）。
2. 改了界面，请在手机宽度（约 390px）和电脑宽度各看一遍，并附截图。
3. 不要提交个人信息：域名、IP、真实姓名、密钥、本机路径。
4. 改了安全相关的地方（登录、同源校验、危险命令规则、提示词里的授权），在 PR 里单独说明。

## 范围

案头想保持“个人在自己电脑上用”的体量：不做多人共用的服务、不做账号体系。想让别人也能用，请各自部署各自的一份。
