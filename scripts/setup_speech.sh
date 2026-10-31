#!/bin/zsh
# 独立环境，不把 MLX / Torch 等依赖塞进案头主进程。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -x "$ROOT/.venv-tts/bin/python" ]]; then
  uv venv --python 3.12 "$ROOT/.venv-tts"
fi
uv pip install --python "$ROOT/.venv-tts/bin/python" 'mlx-audio==0.6.3'
if ! command -v ffmpeg >/dev/null; then
  echo '请先安装 ffmpeg，再运行本脚本。'
  exit 1
fi
"$ROOT/.venv-tts/bin/python" - <<'PY'
from mlx_audio.tts.utils import load_model
model = load_model('mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit')
for result in model.generate_custom_voice(text='你好，案头的普通话朗读已准备好。', speaker='Serena', language='Chinese', stream=True, streaming_interval=.32):
    pass
print('中文预置音色已就绪；日常语音生成使用离线模式。')
PY
