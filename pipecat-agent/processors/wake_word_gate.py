"""
Wake word gate processor for Pipecat.

Pipeline position: transport → vad → wake_gate → stt

Flow:
  1. All AudioRawFrame pass through always (VAD and STT need them).
  2. VADUserStartedSpeakingFrame is held until wake word confirmed.
     - If VAD fires before wake word (continuous speech), the frame is
       buffered and replayed when the gate opens.
  3. After an utterance ends, the gate stays open until active_window
     expires — so the user can ask follow-up questions without saying
     the wake word again.
  4. When the gate expires naturally, the model is reset for fresh detection.
"""

import asyncio
import time

import numpy as np
from loguru import logger
from openwakeword.model import Model

from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# openWakeWord expects 16 kHz mono, 80 ms chunks = 1280 samples
_WAKE_CHUNK_SAMPLES = 1280
# Max age of a buffered VADUserStartedSpeakingFrame before it is discarded
_PENDING_TTL = 2.0


class WakeWordGate(FrameProcessor):
    """
    Gates speech events behind wake word detection.

    After the gate opens (wake word confirmed), it stays open for
    `active_window` seconds — allowing multi-turn conversation without
    repeating the wake word.  The gate closes automatically when the
    window expires, then resets the model for fresh detection.
    """

    def __init__(
        self,
        model_paths: list[str],
        threshold: float = 0.5,
        confirm_chunks: int = 1,
        active_window: float = 8.0,
        inference_framework: str = "onnx",
        on_activated=None,
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
        self._on_activated = on_activated  # async callback → called when gate opens

        # gate state
        self._active_until: float = 0.0   # 0 = closed
        self._speaking: bool = False

        # candidate tracking
        self._candidate_name: str = ""
        self._candidate_score: float = 0.0
        self._confirm_count: int = 0

        # audio buffer for openWakeWord
        self._sample_buffer: np.ndarray = np.array([], dtype=np.int16)

        # buffered VADUserStartedSpeakingFrame (VAD fired before wake word)
        self._pending_started_frame: VADUserStartedSpeakingFrame | None = None
        self._pending_started_at: float = 0.0

        # echo suppression: mic ignored while AI is speaking from speaker
        self._suppress_until: float = 0.0

    # ------------------------------------------------------------------

    def suppress_mic(self, secs: float) -> None:
        """Called by AITuberSink after sending a response to prevent echo."""
        self._suppress_until = time.monotonic() + secs
        logger.info(f"[wake] mic suppressed for {secs:.1f}s (echo prevention)")

    def extend_gate(self) -> None:
        """Extend the active window after an AI response.

        The new window starts from the END of mic suppression, so the user
        always has a full active_window to reply after the AI finishes speaking.
        """
        if self._active_until == 0.0:
            return  # Gate is closed — only wake word can open it
        base = max(self._suppress_until, time.monotonic())
        self._active_until = base + self._active_window
        logger.info(f"[wake] gate extended → {self._active_window:.0f}s from end of AI speech")

    def _is_suppressed(self) -> bool:
        return time.monotonic() < self._suppress_until

    def _is_active(self) -> bool:
        return time.monotonic() < self._active_until

    async def _activate_gate(self, name: str, score: float) -> None:
        """Open the gate and fire the on_activated callback (e.g. play 'はい？')."""
        logger.info(f"[wake] ★ detected: {name}  score={score:.3f}")
        self._active_until = time.monotonic() + self._active_window
        self._reset_candidate()
        # Discard pending frame — wake word audio must not reach STT
        self._pending_started_frame = None

        if self._on_activated:
            # Suppress mic briefly so the ack sound isn't picked up
            self.suppress_mic(3.0)
            asyncio.create_task(self._on_activated())

    def _run_wake_detection(self, audio_bytes: bytes) -> bool:
        """Score audio and return True when the gate should activate."""
        if self._speaking:
            return False

        if self._is_active():
            return False

        # Gate just expired: reset model for fresh detection
        if self._active_until > 0:
            logger.debug("[wake] gate expired — resetting for next wake word")
            self._active_until = 0.0
            self._sample_buffer = np.array([], dtype=np.int16)
            self._model.reset()
            self._reset_candidate()

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
                    self._last_name = self._candidate_name
                    self._last_score = self._candidate_score
                    return True
            else:
                self._reset_candidate()

        return False

    def _reset_candidate(self) -> None:
        self._candidate_name = ""
        self._candidate_score = 0.0
        self._confirm_count = 0

    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame):
            activated = self._run_wake_detection(frame.audio)
            if activated:
                await self._activate_gate(self._last_name, self._last_score)
            await self.push_frame(frame, direction)

        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if self._is_suppressed():
                logger.debug("[wake] speech detected but mic suppressed (AI speaking) — ignoring")
            elif self._is_active():
                logger.info("[wake] speech started — gate open, forwarding to STT")
                self._speaking = True
                await self.push_frame(frame, direction)
            else:
                # Buffer: wake word might still be confirmed during this utterance
                logger.debug("[wake] speech started before wake word — buffering frame")
                self._pending_started_frame = frame
                self._pending_started_at = time.monotonic()

        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._speaking:
                remaining = max(0, self._active_until - time.monotonic())
                logger.info(
                    f"[wake] speech ended — gate stays open ({remaining:.1f}s remaining)"
                )
                self._speaking = False
                self._pending_started_frame = None
                await self.push_frame(frame, direction)
            else:
                # Speech ended without a confirmed wake word (or while suppressed)
                self._pending_started_frame = None

        else:
            await self.push_frame(frame, direction)
