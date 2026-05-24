"""
AITuber Kit output processor for Pipecat.

Streams LLM text to ha-character-bridge sentence-by-sentence as it arrives,
so VOICEVOX can start speaking before the LLM has finished generating.

Sentence boundaries: 。！？\n
Each complete sentence is queued and sent sequentially over WebSocket.

Mic suppression timing is calculated as:
  total_estimated_playback - time_already_elapsed_since_first_sentence_sent
so the gate opens as soon as the AI actually finishes speaking.
"""

import asyncio
import json
import time

import websockets
from loguru import logger
from websockets.exceptions import WebSocketException

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Japanese VOICEVOX speaks at roughly 6 chars/sec
_CHARS_PER_SEC = 6.0
# Minimum suppression even for very short responses
_MIN_SUPPRESS_SECS = 2.0
# Extra buffer for network latency + VOICEVOX startup
_EXTRA_BUFFER_SECS = 2.0

# Characters that mark the end of a speakable sentence
_SENTENCE_ENDERS = frozenset("。！？\n")


class AITuberSink(FrameProcessor):
    """
    Pipecat processor that streams LLM sentences to AITuber Kit as they arrive.

    Instead of waiting for the full LLM response, each sentence is forwarded to
    ha-character-bridge as soon as its terminal punctuation is received.
    Sentences are delivered sequentially via an internal asyncio.Queue so
    AITuber Kit's SpeakQueue receives them in order.

    Mic suppression is applied at LLMFullResponseEndFrame, accounting for
    the time already elapsed since the first sentence was sent, so the gate
    reopens as close as possible to when the AI actually finishes speaking.
    """

    def __init__(self, ws_url: str, default_emotion: str = "neutral", gate=None):
        super().__init__()
        self._ws_url = ws_url
        self._default_emotion = default_emotion
        self._gate = gate
        self._collecting = False
        self._sentence_buf: list[str] = []   # current incomplete sentence
        self._full_buf: list[str] = []        # full response (for suppression calc)
        self._first_send_time: float | None = None  # when first sentence was queued
        self._pending_sends: asyncio.Queue = asyncio.Queue()
        self._send_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Internal sender: runs as a background task, sends sentences in order
    # ------------------------------------------------------------------

    async def _sender_loop(self) -> None:
        """Consume the sentence queue and forward each to AITuber Kit."""
        while True:
            text = await self._pending_sends.get()
            if text is None:  # sentinel — response complete
                break
            await self._send_ws(text)

    async def _send_ws(self, text: str) -> None:
        if not text.strip():
            return
        payload = json.dumps(
            {"text": text, "role": "assistant", "emotion": self._default_emotion, "type": "talk"},
            ensure_ascii=False,
        )
        try:
            async with websockets.connect(self._ws_url, open_timeout=5) as ws:
                await ws.send(payload)
            logger.info(f"[aituber] → {text[:80]}")
        except (WebSocketException, OSError, TimeoutError) as e:
            logger.warning(f"[aituber] websocket error: {e}")

    async def _enqueue_sentence(self, sentence: str) -> None:
        """Queue a sentence for sending, recording the time of the first enqueue."""
        sentence = sentence.strip()
        if not sentence:
            return
        if self._first_send_time is None:
            self._first_send_time = time.monotonic()
        await self._pending_sends.put(sentence)

    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            # Cancel any leftover sender from a previous interrupted turn
            if self._send_task and not self._send_task.done():
                self._send_task.cancel()
            self._pending_sends = asyncio.Queue()
            self._collecting = True
            self._sentence_buf.clear()
            self._full_buf.clear()
            self._first_send_time = None
            self._send_task = asyncio.create_task(self._sender_loop())
            await self.push_frame(frame, direction)

        elif isinstance(frame, TextFrame) and self._collecting:
            for ch in frame.text:
                self._sentence_buf.append(ch)
                self._full_buf.append(ch)
                if ch in _SENTENCE_ENDERS:
                    sentence = "".join(self._sentence_buf)
                    await self._enqueue_sentence(sentence)
                    self._sentence_buf.clear()
            await self.push_frame(frame, direction)

        elif isinstance(frame, LLMFullResponseEndFrame):
            self._collecting = False

            # Flush any trailing text that has no terminal punctuation
            remaining = "".join(self._sentence_buf)
            await self._enqueue_sentence(remaining)
            self._sentence_buf.clear()

            # Signal the sender loop to stop after draining the queue
            await self._pending_sends.put(None)

            # Calculate suppression = remaining playback time only
            # (subtract time already elapsed since first sentence was queued)
            full_text = "".join(self._full_buf).strip()
            self._full_buf.clear()

            if full_text and self._gate is not None:
                total_secs = len(full_text) / _CHARS_PER_SEC + _EXTRA_BUFFER_SECS
                if self._first_send_time is not None:
                    elapsed = time.monotonic() - self._first_send_time
                    suppress_secs = max(_MIN_SUPPRESS_SECS, total_secs - elapsed)
                else:
                    suppress_secs = max(_MIN_SUPPRESS_SECS, total_secs)
                self._gate.suppress_mic(suppress_secs)
                self._gate.extend_gate()
                logger.debug(
                    f"[aituber] suppress={suppress_secs:.1f}s "
                    f"(total={total_secs:.1f}s elapsed={time.monotonic() - (self._first_send_time or time.monotonic()):.1f}s)"
                )

            await self.push_frame(frame, direction)

        else:
            await self.push_frame(frame, direction)
