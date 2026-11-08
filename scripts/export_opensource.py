"""把私有主线导出成开源版「远窗」（anzhuo）：复制 → 改名改品牌 → 去人名 → 删私有文件，能反复跑。

用法：.venv/bin/python scripts/export_opensource.py <开源仓库工作树>
  只拿主线 git 跟踪的文件；导出目录里由开源仓库自己维护的文件（KEEP）原样不动；
  上一轮导出过、这一轮主线已经删掉的文件，在导出目录里也删掉。
导出后在导出目录里：uv lock、跑全量测试（含 tests/test_open_source.py 闸门）、scripts/demo.py 看界面。
"""
from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]

# 私有版独有，不进开源版
EXCLUDE = [
    "src/remote_claude/router.py",            # 分派员：私有版才有（app.py 里按可选导入）
    "src/remote_claude/extensions/course/*",  # 做课扩展
    "tests/test_ext_course.py",
    "tests/test_lan_gateway.py",
    "tests/opensource/conftest.py",           # 闸门在开源版里一直跑，不用开关
    "docs/design.md", "docs/案头-设计方案.md", "docs/给朋友复刻-*", "docs/局域网直连.md",
    "scripts/cert_agent.py", "scripts/check_lan_gateway.py", "scripts/install_lan_gateway.py",
    "scripts/lan_gateway.py", "scripts/mac_local_hosts.py", "scripts/make_profile.py",
    "scripts/export_opensource.py",
    "remote-claude.cmd",
    "README.md", "AGENTS.md",                 # 开源版有自己的，在 KEEP 里
    "uv.lock",                                # 导出后在开源版里 uv lock 重新生成
]

# 开源仓库自己维护：导出不覆盖、不删除
KEEP = [
    "README.md", "README.en.md", "AGENTS.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE",
    "anzhuo.cmd", "docs/promo/*", "uv.lock", ".gitignore",
]

# 路径改名（按顺序）
RENAME = [
    ("src/remote_claude/", "src/anzhuo/"),
    ("scripts/停用小E.command", "scripts/停用远窗.command"),
    ("tests/opensource/test_open_source.py", "tests/test_open_source.py"),
]

# 文本替换（按顺序，先长后短）；正则
SUBS = [
    # 部署痕迹
    (r"https://xiaoe\.ericlab\.cn", "https://your-domain.example.com"),
    (r"xiaoe\.ericlab\.cn", "your-domain.example.com"),
    (r"ericlab-aliyun", "my-server"),
    (r"com\.zhuangyi\.remote-claude", "local.anzhuo"),
    # 人名：写进注释里的“某天庄毅定”只留日期
    (r" ?庄毅(定|同意)", r"\1"),
    (r"庄毅：", ""),
    (r"按庄毅的", "按作者的"),
    (r"庄老师", ""),
    (r"庄毅", "作者"),
    # 包名、命令名、环境变量、品牌
    (r"REMOTE_CLAUDE_", "ANZHUO_"),
    (r"remote_claude", "anzhuo"),
    (r"remote-claude", "anzhuo"),
    (r"xiaoe", "anzhuo"),
    (r"小E (?=[\u4e00-\u9fff])", "远窗"),   # 中文前后不留空格：“小E 已停用” → “远窗已停用”
    (r"(?<=[\u4e00-\u9fff]) 小E", "远窗"),
    (r"小E", "远窗"),
]

# 个别文件的定点改写
FILE_SUBS = {
    "tests/test_open_source.py": [
        (r'"""开源导出的底线（默认不跑；ANZHUO_EXPORT_CHECK=1 时才跑，见同目录 conftest.py）：',
         '"""开源版的底线：'),
        (r"# 默认查这个仓库；查导出目录时设 ANZHUO_EXPORT_ROOT（只用来扫文件，代码照旧从本仓库导入）\n",
         ""),
        (r'ROOT = Path\(os.environ.get\("ANZHUO_EXPORT_ROOT"\) or Path\(__file__\).resolve\(\).parents\[2\]\)',
         "ROOT = Path(__file__).resolve().parents[1]"),
    ],
    "pyproject.toml": [
        (r'name = "anzhuo"\nversion = "[^"]*"\ndescription = "[^"]*"',
         'name = "anzhuo"\nversion = "{version}"\ndescription = "远窗：在手机或网页上给电脑里的 Claude Code / Codex 派活、收结果"'),
        (r"authors = \[\n(    .*\n)*\]\n", 'license = { text = "MIT" }\n'),
    ],
    # 分派员不进开源版：档位只按文件夹定，也没有 auto_route
    "config.example.toml": [
        (r"新开对话没指定模型时，分派员（Haiku）先判断这件事是哪一档，再照下表交给谁",
         "新开对话没指定模型时，代码仓里按开发档、别处按简单任务档，照下表交给谁"),
        (r"# auto_route = true .*\n", ""),
    ],
    "docs/配置参考.md": [
        (r"新开对话没指定模型时，分派员先判断属于哪一档；判断不出来时，代码仓里按开发算，别处按简单任务。",
         "新开对话没指定模型时，代码仓里按开发档挑，别处按简单任务档。"),
    ],
    "src/anzhuo/static/app/list.js": [(r"const OWNER = '';", "const OWNER = '';   // 想在问候里带称呼就填上，比如 '张老师'")],
}

TEXT_SUFFIX = {".py", ".js", ".css", ".html", ".md", ".toml", ".sh", ".command", ".cmd", ".json", ".txt", ".mjs", ".cfg"}


def match(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def tracked(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def rename(path: str) -> str:
    for a, b in RENAME:
        if path == a or (a.endswith("/") and path.startswith(a)):
            path = b + path[len(a):]
    return path


def transform(text: str, dest_path: str, version: str) -> str:
    for a, b in SUBS:
        text = re.sub(a, b, text)
    for a, b in FILE_SUBS.get(dest_path, []):
        text, n = re.subn(a, b.replace("{version}", version), text)
        if not n:
            raise SystemExit(f"{dest_path}：定点改写没对上，主线改过这里？\n  {a}")
    return text


def current_version(dest: Path) -> str:
    m = re.search(r'^version = "([^"]+)"', (dest / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    dest = Path(sys.argv[1]).expanduser().resolve()
    if dest == SRC or not (dest / ".git").exists():
        raise SystemExit(f"{dest} 不是开源仓库的工作树")
    version = current_version(dest)
    produced: set[str] = set()
    for path in tracked(SRC):
        if match(path, EXCLUDE) or not (SRC / path).is_file():
            continue
        out = rename(path)
        if match(out, KEEP):
            continue
        target = dest / out
        target.parent.mkdir(parents=True, exist_ok=True)
        if (SRC / path).suffix.lower() in TEXT_SUFFIX:
            target.write_text(transform((SRC / path).read_text(encoding="utf-8"), out, version), encoding="utf-8")
        else:
            shutil.copy2(SRC / path, target)
        shutil.copymode(SRC / path, target)
        produced.add(out)
    removed = [p for p in tracked(dest) if p not in produced and not match(p, KEEP) and (dest / p).exists()]
    for p in removed:
        (dest / p).unlink()
    for d in sorted({(dest / p).parent for p in removed}, key=lambda x: -len(x.parts)):
        while d != dest and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent
    print(f"导出 {len(produced)} 个文件到 {dest}（版本 {version}），删掉 {len(removed)} 个主线已没有的文件")
    for p in removed:
        print("  删", p)


if __name__ == "__main__":
    main()
