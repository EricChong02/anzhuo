"""独立语音进程，仅由 .venv-tts 启动；stdout 只输出进度，不记录正文。"""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

PREFIX = 'ANTOU_TTS '
MODEL = 'mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit'


def emit(**data):
    print(PREFIX + json.dumps(data), flush=True)


def kokoro_parts(text, phonemize, limit=480):
    """按音素预算递归分段，不能依赖库静默截断到 510 个字符。"""
    if len(phonemize(text)) <= limit:
        return [text]
    if len(text) < 2:
        raise ValueError('单个字符的发音超出模型限制')
    mid = len(text) // 2
    boundaries = [i + 1 for i, c in enumerate(text) if c in '，。！？；、,;!? ' and len(text) // 4 <= i < len(text) * 3 // 4]
    cut = min(boundaries, key=lambda n: abs(n - mid)) if boundaries else mid
    return kokoro_parts(text[:cut], phonemize, limit) + kokoro_parts(text[cut:], phonemize, limit)


def generate_parts(model, parts):
    if 'Qwen3-TTS' in MODEL:
        for part in parts:
            yield from model.generate_custom_voice(text=part, speaker='Serena', language='Chinese',
                                                  stream=True, streaming_interval=.32, max_tokens=4096,
                                                  temperature=.7)
        return
    # 保留 Kokoro 后备 / 对照路径，并按真实音素数保护长段落。
    def phonemize(text):
        prepared = text
        if re.search(r'[A-Za-z]', text):
            english = model._get_pipeline('a').g2p
            prepared = re.sub(r"[A-Za-z]+(?:[ '\-][A-Za-z]+)*", lambda match: english(match[0])[0] or match[0], text)
        return model._get_pipeline('z').g2p(prepared)[0]
    for original in parts:
        for part in kokoro_parts(original, phonemize):
            if re.search(r'[A-Za-z]', part):
                english = model._get_pipeline('a').g2p
                part = re.sub(r"[A-Za-z]+(?:[ '\-][A-Za-z]+)*", lambda match: english(match[0])[0] or match[0], part)
            yield from model.generate(text=part, voice='zf_xiaoxiao', lang_code='z', speed=1.0)


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
            count, last_size, seconds = 0, 0, 0.0
            encoder = None
            try:
                with (out / 'audio.part').open('wb') as audio:
                    # 整段只启动一次编码器，避免流式小块反复编码产生停顿和额外延迟。
                    encoder = subprocess.Popen([ffmpeg, '-v', 'error', '-probesize', '32', '-analyzeduration', '0',
                                                '-f', 'f32le', '-ar', '24000', '-ac', '1', '-i', 'pipe:0',
                                                '-c:a', 'libmp3lame', '-b:a', '64k', '-write_xing', '0',
                                                '-id3v2_version', '0', '-map_metadata', '-1', '-flush_packets', '1',
                                                '-f', 'mp3', 'pipe:1'], stdin=subprocess.PIPE,
                                               stdout=audio, stderr=subprocess.DEVNULL)
                    for result in generate_parts(model, req['parts']):
                        if result.sample_rate != 24000:
                            raise RuntimeError('unexpected sample rate')
                        raw = np.asarray(result.audio, dtype=np.float32).tobytes()
                        seconds += len(raw) / 4 / 24000
                        if seconds * 8000 > 128 * 1024 ** 2:
                            raise RuntimeError('audio limit')
                        encoder.stdin.write(raw)
                        encoder.stdin.flush()
                        count += 1
                        size = (out / 'audio.part').stat().st_size
                        if size > last_size:
                            emit(duration=size / 8000, parts=count)
                            last_size = size
                    encoder.stdin.close()
                    if encoder.wait(timeout=60) != 0:
                        raise RuntimeError('encode')
                size = (out / 'audio.part').stat().st_size
                if not size or size > 128 * 1024 ** 2:
                    raise RuntimeError('audio limit')
                emit(duration=size / 8000, parts=count)
            finally:
                if encoder and encoder.poll() is None:
                    encoder.kill()
                    encoder.wait()
            emit(done=True)
        except Exception:
            emit(error=True)


if __name__ == '__main__':
    run()
