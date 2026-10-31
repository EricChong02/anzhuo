"""独立语音进程，仅由 .venv-tts 启动；stdout 只输出进度，不记录正文。"""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

PREFIX = 'ANTOU_TTS '
MODEL = 'mlx-community/Kokoro-82M-bf16'


def emit(**data):
    print(PREFIX + json.dumps(data), flush=True)


def run():
    # 模型与音色由安装脚本预取；日常运行不把正文发给外部服务。
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    import numpy as np
    from mlx_audio.tts.utils import load_model
    model = load_model(MODEL)
    ffmpeg = shutil.which('ffmpeg') or str(Path.home() / '.local/bin/ffmpeg')
    for line in sys.stdin:
        try:
            req = json.loads(line)
            out = Path(req['out'])
            duration, count = 0.0, 0
            with (out / 'audio.part').open('wb') as audio:
                for part in req['parts']:
                    if re.search(r'[A-Za-z]', part):
                        english = model._get_pipeline('a').g2p
                        part = re.sub(r"[A-Za-z]+(?:[ '\-][A-Za-z]+)*", lambda match: english(match[0])[0] or match[0], part)
                    for result in model.generate(text=part, voice='zf_xiaobei', lang_code='z', speed=1.0):
                        raw = np.asarray(result.audio, dtype=np.float32).tobytes()
                        # 每段用相同参数编码，不插入文件头，可连续拼成可播放的 MP3。
                        proc = subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'f32le', '-ar', str(result.sample_rate),
                                               '-ac', '1', '-i', 'pipe:0', '-c:a', 'libmp3lame', '-b:a', '64k',
                                               '-write_xing', '0', '-id3v2_version', '0', '-map_metadata', '-1',
                                               str(out / 'segment.mp3')], input=raw, capture_output=True, timeout=60)
                        if proc.returncode:
                            raise RuntimeError('encode')
                        chunk = (out / 'segment.mp3').read_bytes()
                        if audio.tell() + len(chunk) > 128 * 1024 ** 2:
                            raise RuntimeError('audio limit')
                        audio.write(chunk)
                        audio.flush()
                        duration += len(chunk) / 8000  # 64kbps CBR，包含每段编码填充
                        count += 1
                        emit(duration=duration, parts=count)
            (out / 'segment.mp3').unlink(missing_ok=True)
            emit(done=True)
        except Exception:
            emit(error=True)


if __name__ == '__main__':
    run()
