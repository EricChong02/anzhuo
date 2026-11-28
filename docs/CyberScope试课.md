# 用远窗试课和改课

远窗默认提供远程工作功能。你可以连接自己的 Claude Code、Codex，或用 DeepSeek 作为 Claude Code 的后端。使用 CyberScope 备课的老师可以开启试课模块，用手机或电脑预览课件、反馈某一屏，再让远窗里的对话修改课程。

## 在自己的电脑上启用

先启动 CyberScope 的做课服务，在电脑本机打开一门课的试课页。确认网页可以加载课件，并找到该服务 `/workspace/` 对应的课程根目录。下面用端口 `8000` 和 `~/Courses` 举例，请换成自己的设置。

在远窗的 `config.toml` 增加下面一节，再重启远窗。远窗需要独立站点根路径，`web_prefix` 留空。公开访问沿用远窗的 HTTPS 和登录设置。

```toml
[ext.course]
enabled = true
upstream = "http://127.0.0.1:8000"
root = "~/Courses"
```

登录远窗后，从侧栏的「课」打开草稿或已上架课程。试课页的「反馈这一屏」会把课名、节、屏和你附上的截图交给远窗的做课对话。AI 修改完成后，回复里的试课链接可以回到对应位置。也可以先就这一屏提问。

如需管理员在试课页直接修改文字或布局，可以在 `[ext.course]` 加上 `allow_text_edit = true`。这会直接写入本机课件。预览代理只连接你配置的本机 CyberScope，开放课件、预览资源和所需的判题与草稿列表接口。教师台、学生数据和上课连接继续走 CyberScope 自己的入口。

## 邀请一位老师一起改课

如果老师用自己的电脑运行远窗和 CyberScope，可以按上面的步骤启用自己的试课模块。如果老师要加入你这台电脑上的项目，在 macOS 上为其创建远窗受限账号，并把账号绑定到一门课。下面假设该课的文件夹是课程根下的 `_drafts/lesson-a`。

```toml
[accounts.owner]
role = "admin"

[accounts.teacher]
role = "user"
course = "_drafts/lesson-a"
root = "~/Courses/_drafts/lesson-a"
share = 0.2
```

分别运行 `scripts/set_account.py owner` 和 `scripts/set_account.py teacher` 设置密码。账号已有同名配置时，把新字段放到原配置节里。重启后，管理员把该课的试课链接发给老师。老师用自己的账号登录，可以预览这一课、提交反馈和查看自己的做课对话。反馈在该账号的课程目录内执行。课程上架移动目录后，同步更新该账号的 `course` 和 `root`。远窗账号的设置见[配置参考](配置参考.md)。
