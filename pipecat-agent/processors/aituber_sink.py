"""
AITuber Kit output processor for Pipecat.

Collects LLM text between LLMFullResponseStartFrame and LLMFullResponseEndFrame,
then sends the complete text to ha-character-bridge via WebSocket.
ha-character-bridge broadcasts to AITuber Kit, which speaks via VOICEVOX.
"""

import asyncio
import json

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


class AITuberSink(FrameProcessor):
    """
    Pipecat processor that forwards LLM text output to AITuber Kit.

    Collects streamed TextFrame tokens between LLMFullResponseStartFrame /
    LLMFullResponseEndFrame, then sends a single WebSocket message to
    ha-character-bridge so AITuber Kit can speak the response via VOICEVOX.
    """

    def __init__(self, ws_url: str, default_emotion: str = "neutral"):
        super().__init__()
        self._ws_url = ws_url
        self._default_emotion = default_emotion
        self._collecting = False
        self._buffer: list[str] = []

    async def _send(self, text: str, emotion: str = "neutral") -> None:
        if not text.strip():
            return
        payload = json.dumps({"text": text, "emotion": emotion}, ensure_ascii=False)
        try:
            async with websockets.connect(self._ws_url, open_timeout=5) as ws:
                await ws.send(payload)
            logger.info(f"[aituber] → {text[:80]}")
        except (WebSocketException, OSError, TimeoutError) as e:
            logger.warning(f"[aituber] websocket error: {e}")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._collecting = True
            self._buffer.clear()
            await self.push_frame(frame, direction)

        elif isinstance(frame, TextFrame) and self._collecting:
            self._buffer.append(frame.text)
            await self.push_frame(frame, direction)

        elif isinstance(frame, LLMFullResponseEndFrame):
            self._collecting = False
            full_text = "".join(self._buffer).strip()
            self._buffer.clear()
            if full_text:
                # fire-and-forget so the pipeline doesn't block on WebSocket latency
                asyncio.create_task(self._send(full_text, self._default_emotion))
            await self.push_frame(frame, direction)

        else:
            await self.push_frame(frame, direction)
