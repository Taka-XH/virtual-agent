"""
Wake word gate processor for Pipecat.

Audio frames always pass through (VAD and STT need them).
UserStartedSpeakingFrame is held until the wake word has been detected,
so STT only activates after the user says the wake word.
"""

import time

import numpy as np
from loguru import logger
from openwakeword.model import Model

from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# openWakeWord は 16kHz mono で 80ms チャンク (1280 samples) を期待する
_WAKE_CHUNK_SAMPLES = 1280


class WakeWordGate(FrameProcessor):
    """
    Pipecat processor that gates speech events behind wake word detection.

    - All AudioRawFrame pass through unconditionally (VAD needs them).
    - UserStartedSpeakingFrame is forwarded only when the wake word was
      recently detected (within `active_window` seconds).
    - UserStoppedSpeakingFrame is forwarded only if a speaking session
      was started (i.e. after the gate opened).
    - After each utterance the gate resets to wake-word-wait mode.
    """

    def __init__(
        self,
        model_paths: list[str],
        threshold: float = 0.5,
        confirm_chunks: int = 2,
        active_window: float = 8.0,
        inference_framework: str = "onnx",
    ):
        super().__init__()
        logger.info(f"[wake] loading model(s): {model_paths}")
        self._model = Model(
            wakeword_models=model_paths,
            inference_framework=inference_framework,
        )
        self._threshold = threshold
        self._confirm_chunks = confirm_chunks
        self._active_window = active_window

        # gate state
        self._active_until: float = 0.0
        self._speaking: bool = False

        # candidate tracking (like voice_listener.py)
        self._candidate_name: str = ""
        self._candidate_score: float = 0.0
        self._confirm_count: int = 0

        # rolling buffer for feeding openWakeWord in correct chunk sizes
        self._sample_buffer: np.ndarray = np.array([], dtype=np.int16)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_active(self) -> bool:
        return time.monotonic() < self._active_until

    def _run_wake_detection(self, audio_bytes: bytes) -> None:
        """Feed audio to openWakeWord; activate gate when wake word confirmed."""
        if self._is_active() or self._speaking:
            return

        audio_np = np.frombuffer(audio_bytes, dtype=np.int16)
        self._sample_buffer = np.concatenate([self._sample_buffer, audio_np])

        while len(self._sample_buffer) >= _WAKE_CHUNK_SAMPLES:
            chunk = self._sample_buffer[:_WAKE_CHUNK_SAMPLES]
            self._sample_buffer = self._sample_buffer[_WAKE_CHUNK_SAMPLES:]

            prediction = self._model.predict(chunk)
            if not prediction:
                continue

            best_name, best_score = max(prediction.items(), key=lambda kv: kv[1])

            if best_score >= self._threshold:
                if best_name == self._candidate_name:
                    self._confirm_count += 1
                    self._candidate_score = max(self._candidate_score, best_score)
                else:
                    self._candidate_name = best_name
                    self._candidate_score = best_score
                    self._confirm_count = 1

                if self._confirm_count >= self._confirm_chunks:
                    logger.info(
                        f"[wake] detected: {self._candidate_name} "
                        f"score={self._candidate_score:.3f} confirms={self._confirm_count}"
                    )
                    self._active_until = time.monotonic() + self._active_window
                    self._reset_candidate()
            else:
                self._reset_candidate()

    def _reset_candidate(self) -> None:
        self._candidate_name = ""
        self._candidate_score = 0.0
        self._confirm_count = 0

    def _reset_gate(self) -> None:
        self._active_until = 0.0
        self._speaking = False
        self._sample_buffer = np.array([], dtype=np.int16)
        self._model.reset()
        self._reset_candidate()

    # ------------------------------------------------------------------
    # FrameProcessor interface
    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame):
            self._run_wake_detection(frame.audio)
            # Always pass audio so VAD / STT can process it
            await self.push_frame(frame, direction)

        elif isinstance(frame, UserStartedSpeakingFrame):
            if self._is_active():
                logger.info("[wake] speech started — gate open")
                self._speaking = True
                await self.push_frame(frame, direction)
            else:
                logger.debug("[wake] speech detected but wake word not active — ignoring")

        elif isinstance(frame, UserStoppedSpeakingFrame):
            if self._speaking:
                logger.info("[wake] speech ended — resetting to wake-word-wait")
                self._speaking = False
                self._reset_gate()
                await self.push_frame(frame, direction)

        else:
            await self.push_frame(frame, direction)
