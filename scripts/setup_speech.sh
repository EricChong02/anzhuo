#!/bin/zsh
# 独立环境，不把 MLX / Torch 等依赖塞进案头主进程。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -x "$ROOT/.venv-tts/bin/python" ]]; then
  uv venv --python 3.12 "$ROOT/.venv-tts"
fi
uv pip install --python "$ROOT/.venv-tts/bin/python" 'mlx-audio==0.6.3' 'misaki[zh,en]==0.9.4' \
  'https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl'
if ! command -v ffmpeg >/dev/null; then
  echo '请先安装 ffmpeg，再运行本脚本。'
  exit 1
fi
"$ROOT/.venv-tts/bin/python" - <<'PY'
from mlx_audio.tts.utils import load_model
model = load_model('mlx-community/Kokoro-82M-bf16')
for result in model.generate(text='你好，案头的本地朗读已准备好。', voice='zf_xiaobei', lang_code='z'):
    pass
model._get_pipeline('a').g2p('PDF and Claude')
print('中文、英文发音资源已就绪；日常语音生成使用离线模式。')
PY
