"""Sınama için Piper arayüzü taklidi (gerçek ses üretmez, belirli uzunlukta PCM verir)."""
import math
import struct


class _Cfg:
    sample_rate = 22050


class _Chunk:
    def __init__(self, data):
        self.audio_int16_bytes = data
        self.sample_rate = 22050


class SynthesisConfig:
    def __init__(self, length_scale=1.0):
        self.length_scale = length_scale


class PiperVoice:
    config = _Cfg()

    @classmethod
    def load(cls, path, config_path=None, use_cuda=False):
        open(path, "rb").close()
        open(config_path, "rb").close()
        return cls()

    def synthesize(self, text, syn_config=None):
        n = 2205 * max(1, len(text) // 4)
        for i in range(0, n, 2205):
            yield _Chunk(b"".join(struct.pack("<h", int(4000 * math.sin(j / 12))) for j in range(i, i + 2205)))
