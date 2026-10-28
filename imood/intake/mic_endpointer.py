"""VAD endpointing for the microphone path: PCM16 16 kHz mono chunks in, one Utterance out per finished sentence.

Classes: EndpointConfig (thresholds), Utterance (one finished sentence), Endpointer (feed chunks, get utterances).
Speech recognition is in intake/transcriber.py. Needs webrtcvad (package webrtcvad-wheels).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger("mic_endpointer")

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit
BYTES_PER_MS = SAMPLE_RATE * SAMPLE_WIDTH // 1000  # 32


@dataclass
class EndpointConfig:
    frame_ms: int = 20                 # webrtcvad 子 frame（10/20/30 擇一）
    vad_aggressiveness: int = 2        # 0–3，室內安靜環境的平衡點
    end_silence_ms: int = 900          # 連續靜音多久算「講完一句」（700 對朗讀長句偏短，見 asr-42-vad-plan 第 7 節）
    min_utterance_ms: int = 400        # 有聲音訊不足這麼長就丟棄（濾咳嗽/誤觸）
    max_utterance_ms: int = 15000      # 硬上限，避免 VAD 卡噪音時 buffer 無限長
    pre_pad_ms: int = 300              # 保留語音起點前這麼多音訊，不切掉字頭
    calibration_ms: int = 500          # 連線後拿前這麼多音訊估環境底噪
    energy_gate_factor: float = 3.0    # chunk RMS < floor * factor 無條件當靜音
    min_noise_floor: float = 60.0      # 底噪 RMS 下限，避免絕對安靜時 factor 失效


@dataclass
class Utterance:
    pcm: bytes
    duration_ms: float
    voiced_ms: float
    trailing_silence_ms: float


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


class Endpointer:
    """
    每個 WebSocket 連線一份。feed() 進 PCM chunk，回傳 None（還沒講完）或
    Utterance（這句話講完了，pcm 是整段音訊）。太短的語句回傳 None 並在
    內部記 log。
    """

    def __init__(self, config: Optional[EndpointConfig] = None):
        import webrtcvad

        self.cfg = config or EndpointConfig()
        self._vad = webrtcvad.Vad(self.cfg.vad_aggressiveness)
        self._frame_bytes = self.cfg.frame_ms * BYTES_PER_MS
        self._pre_pad_bytes = self.cfg.pre_pad_ms * BYTES_PER_MS

        self._carry = bytearray()       # 上個 chunk 沒湊滿一個 frame 的尾巴
        self._prepad = bytearray()      # 還沒觸發時的滾動 pre-roll
        self._buf = bytearray()         # 觸發後累積的整句音訊
        self._triggered = False
        self._silence_ms = 0.0
        self._voiced_ms = 0.0
        self._idle_silence_ms = 0.0     # 上一句 emit 之後、還沒開始下一句的靜音長度

        self._calib = bytearray()
        self._noise_floor: Optional[float] = None

    # -- 內部 --------------------------------------------------

    def _calibrated(self) -> bool:
        return self._noise_floor is not None

    def _do_calibration(self, chunk: bytes) -> None:
        need = self.cfg.calibration_ms * BYTES_PER_MS
        self._calib.extend(chunk)
        if len(self._calib) >= need:
            samples = np.frombuffer(bytes(self._calib[:need]), dtype=np.int16)
            self._noise_floor = max(_rms(samples), self.cfg.min_noise_floor)
            logger.info("noise floor calibrated: %.1f", self._noise_floor)
            # 校正用掉的音訊當作 pre-roll 尾段留著
            leftover = bytes(self._calib[max(0, len(self._calib) - self._pre_pad_bytes):])
            self._prepad.extend(leftover)
            self._trim_prepad()
            self._calib.clear()

    def _trim_prepad(self) -> None:
        if len(self._prepad) > self._pre_pad_bytes:
            del self._prepad[: len(self._prepad) - self._pre_pad_bytes]

    def _frame_is_voiced(self, frame: bytes) -> bool:
        try:
            speech = self._vad.is_speech(frame, SAMPLE_RATE)
        except Exception:
            return False
        if not speech:
            return False
        # 能量門檻：太安靜就算 webrtcvad 說有聲也當靜音
        samples = np.frombuffer(frame, dtype=np.int16)
        return _rms(samples) >= self._noise_floor * self.cfg.energy_gate_factor

    def _emit(self) -> Optional[Utterance]:
        pcm = bytes(self._buf)
        voiced_ms = self._voiced_ms
        trailing = self._silence_ms
        duration_ms = len(pcm) / BYTES_PER_MS

        self._buf.clear()
        self._prepad.clear()
        self._triggered = False
        self._silence_ms = 0.0
        self._voiced_ms = 0.0
        self._idle_silence_ms = 0.0  # 開始數這一句之後的靜音（給續句合併判斷用）

        if voiced_ms < self.cfg.min_utterance_ms:
            logger.info("utterance discarded: only %.0fms voiced (< %d)",
                        voiced_ms, self.cfg.min_utterance_ms)
            return None
        return Utterance(pcm=pcm, duration_ms=duration_ms,
                         voiced_ms=voiced_ms, trailing_silence_ms=trailing)

    # -- 對外 --------------------------------------------------

    @property
    def triggered(self) -> bool:
        """目前是否在一句話的中間（已偵測到語音、還沒到句尾靜音）。"""
        return self._triggered

    @property
    def silence_since_last_ms(self) -> float:
        """上一句結束後累積了多久的靜音（還沒開始下一句時才有意義）。"""
        return self._idle_silence_ms

    def feed(self, chunk: bytes) -> Optional[Utterance]:
        if not self._calibrated():
            self._do_calibration(chunk)
            return None

        data = bytes(self._carry) + chunk
        n_frames = len(data) // self._frame_bytes
        self._carry = bytearray(data[n_frames * self._frame_bytes:])

        result: Optional[Utterance] = None
        for i in range(n_frames):
            frame = data[i * self._frame_bytes:(i + 1) * self._frame_bytes]
            voiced = self._frame_is_voiced(frame)

            if not self._triggered:
                self._prepad.extend(frame)
                self._trim_prepad()
                if voiced:
                    # 觸發：把 pre-roll 一起帶進 buffer
                    self._buf.extend(self._prepad)
                    self._prepad.clear()
                    self._triggered = True
                    self._voiced_ms = self.cfg.frame_ms
                    self._silence_ms = 0.0
                    self._idle_silence_ms = 0.0
                else:
                    self._idle_silence_ms += self.cfg.frame_ms
                continue

            # 已觸發
            self._buf.extend(frame)
            if voiced:
                self._voiced_ms += self.cfg.frame_ms
                self._silence_ms = 0.0
            else:
                self._silence_ms += self.cfg.frame_ms

            over_silence = self._silence_ms >= self.cfg.end_silence_ms
            over_max = len(self._buf) / BYTES_PER_MS >= self.cfg.max_utterance_ms
            if over_silence or over_max:
                if over_max and not over_silence:
                    logger.info("utterance force-flushed at max %dms", self.cfg.max_utterance_ms)
                emitted = self._emit()
                if emitted is not None:
                    result = emitted
                    # 一個 chunk 內通常只會結束一句；剩下的 frame 併入下一輪
                    self._carry = bytearray(data[(i + 1) * self._frame_bytes:])
                    break
        return result

    def reset(self) -> None:
        self.__init__(self.cfg)
