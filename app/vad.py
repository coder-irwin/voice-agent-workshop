"""
vad.py — Smart WebRTC-based Voice Activity Detector.

Uses Google's WebRTC VAD to reliably filter out non-speech noise (typing, static)
and only trigger on actual human voice patterns.

Algorithm:
  1. Buffer incoming PCM bytes into strict 30ms frames (960 bytes at 16kHz).
  2. Feed frames to webrtcvad (Aggressiveness mode 3).
  3. Maintain speech state with onset and grace periods to prevent stuttering.
"""

import webrtcvad
import numpy as np
import logging

logger = logging.getLogger(__name__)

class EnergyVAD:
    """
    WebRTC-based VAD for highly accurate human voice detection.

    Parameters
    ----------
    sample_rate      : int   — audio sample rate (must be 8000, 16000, 32000, or 48000)
    aggressiveness   : int   — 0 to 3 (3 is most aggressive at filtering out non-speech noise)
    onset_frames     : int   — consecutive speech frames needed to flip SPEAKING
    grace_frames     : int   — consecutive silent frames allowed before flipping SILENCE
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        aggressiveness: int = 3,
        onset_frames: int = 2,
        grace_frames: int = 12, # 12 * 30ms = 360ms of grace silence
        min_volume_threshold: float = 1200.0, # High RMS threshold for primary speaker
    ):
        self.sample_rate = sample_rate
        self.onset_frames = onset_frames
        self.grace_frames = grace_frames
        self.min_volume_threshold = min_volume_threshold
        
        self.vad = webrtcvad.Vad(aggressiveness)
        self._buffer = bytearray()
        
        # 30ms frame size calculation
        # 16000 Hz * 0.03 seconds = 480 samples. 16-bit PCM = 2 bytes per sample -> 960 bytes.
        self.frame_bytes = int(self.sample_rate * 0.03 * 2)

        # State
        self._speaking: bool = False
        self._onset_count: int = 0
        self._grace_count: int = 0

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    def process(self, pcm_bytes: bytes) -> bool:
        """
        Feed any length of raw PCM int16 bytes.
        Automatically chunks into 30ms frames and returns True if speech is active.
        """
        self._buffer.extend(pcm_bytes)
        
        # Process all available full 30ms frames in the buffer
        while len(self._buffer) >= self.frame_bytes:
            frame = bytes(self._buffer[:self.frame_bytes])
            del self._buffer[:self.frame_bytes]
            
            try:
                is_active = self.vad.is_speech(frame, self.sample_rate)
                
                # Primary Voice Volume Gate
                if is_active and self.min_volume_threshold > 0:
                    audio_array = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
                    rms = float(np.sqrt(np.mean(audio_array ** 2)) + 1e-8)
                    if rms < self.min_volume_threshold:
                        is_active = False # Ignored: background speaker
                        
            except Exception as e:
                logger.error(f"VAD error: {e}")
                is_active = False

            if is_active:
                self._grace_count = 0
                self._onset_count += 1
                if not self._speaking and self._onset_count >= self.onset_frames:
                    self._speaking = True
                    logger.debug("VAD: speech onset detected")
            else:
                self._onset_count = 0
                if self._speaking:
                    self._grace_count += 1
                    if self._grace_count >= self.grace_frames:
                        self._speaking = False
                        self._grace_count = 0
                        logger.debug("VAD: silence detected")

        return self._speaking

    def reset(self):
        """Reset VAD state (call on session disconnect)."""
        self._buffer.clear()
        self._speaking = False
        self._onset_count = 0
        self._grace_count = 0
