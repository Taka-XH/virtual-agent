"""
Pipecat-based voice AI agent for V_agent.

Pipeline:
  LocalAudioTransport (mic + Silero VAD)
    → WakeWordGate        (openWakeWord: "Hey Kemy")
    → OpenAISTTService    (gpt-4o-transcribe, streaming via VAD events)
    → context aggregator  (user turn)
    → OpenAILLMService    (gpt-4o, function calling for HA)
    → AITuberSink         (send text → ha-character-bridge → AITuber Kit → VOICEVOX)
    → context aggregator  (assistant turn)

Latency improvements vs the old system:
- VAD + STT are handled in the same streaming pipeline (no separate recording step)
- LLM starts as soon as STT produces a TranscriptionFrame
- AITuber Kit receives text the moment the LLM finishes
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.openai import OpenAILLMService, OpenAISTTService
from pipecat.transports.local.audio import LocalAudioParams, LocalAudioTransport

from ha_tools import HA_TOOL_DEFINITIONS, run_ha_action
from processors.aituber_sink import AITuberSink
from processors.wake_word_gate import WakeWordGate

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")


def _resolve_model_paths(raw: str) -> list[str]:
    """Resolve comma-separated model paths relative to APP_DIR."""
    paths = []
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        resolved = (APP_DIR / p).resolve() if not Path(p).is_absolute() else Path(p)
        paths.append(str(resolved))
    return paths


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-transcribe")

HA_CHAR_BRIDGE_WS_URL = os.getenv("HA_CHAR_BRIDGE_WS_URL", "ws://127.0.0.1:8000/ws")

WAKEWORD_MODEL_PATHS = _resolve_model_paths(
    os.getenv("WAKEWORD_MODEL_PATHS", "../voice-listener/models/Hey_Kemy.onnx")
)
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
WAKE_CONFIRM_CHUNKS = int(os.getenv("WAKE_CONFIRM_CHUNKS", "2"))
WAKE_ACTIVE_WINDOW = float(os.getenv("WAKE_ACTIVE_WINDOW", "8.0"))

MIC_DEVICE_INDEX_RAW = os.getenv("MIC_DEVICE_INDEX", "").strip()
MIC_DEVICE_INDEX: int | None = int(MIC_DEVICE_INDEX_RAW) if MIC_DEVICE_INDEX_RAW else None

SYSTEM_PROMPT = """あなたは家のAIキャラクターです。ユーザーと日本語で自然な会話をしてください。
家電操作を頼まれたら、利用可能なツールを使って実行してください。
返答は短く、自然で親しみやすいトーンで話してください。"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY が .env に設定されていません")

    # --- Transport (mic + Silero VAD) ---
    transport = LocalAudioTransport(
        params=LocalAudioParams(
            audio_in_enabled=True,
            audio_out_enabled=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            vad_audio_passthrough=True,
            input_device_index=MIC_DEVICE_INDEX,
        )
    )

    # --- STT ---
    stt = OpenAISTTService(
        api_key=OPENAI_API_KEY,
        model=STT_MODEL,
        language="ja",
    )

    # --- LLM ---
    llm = OpenAILLMService(
        api_key=OPENAI_API_KEY,
        model=LLM_MODEL,
    )

    context = OpenAILLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=HA_TOOL_DEFINITIONS,
    )
    context_aggregator = llm.create_context_aggregator(context)

    # HA function call handler
    async def handle_run_ha_action(
        function_name: str,
        tool_call_id: str,
        arguments: dict,
        llm,  # noqa: ARG001
        context,  # noqa: ARG001
        result_callback,
    ) -> None:
        action = arguments.get("action", "")
        result = await asyncio.to_thread(run_ha_action, action)
        await result_callback(result)

    llm.register_function("run_ha_action", handle_run_ha_action)

    # --- Wake word gate ---
    wake_gate = WakeWordGate(
        model_paths=WAKEWORD_MODEL_PATHS,
        threshold=WAKE_THRESHOLD,
        confirm_chunks=WAKE_CONFIRM_CHUNKS,
        active_window=WAKE_ACTIVE_WINDOW,
    )

    # --- AITuber Kit output ---
    aituber_sink = AITuberSink(ws_url=HA_CHAR_BRIDGE_WS_URL)

    # --- Pipeline ---
    pipeline = Pipeline(
        [
            transport.input(),
            wake_gate,
            stt,
            context_aggregator.user(),
            llm,
            aituber_sink,
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(pipeline)
    runner = PipelineRunner()

    logger.info("=" * 60)
    logger.info("Pipecat Voice Agent 起動")
    logger.info(f"  STT model : {STT_MODEL}")
    logger.info(f"  LLM model : {LLM_MODEL}")
    logger.info(f"  Wake word : {WAKEWORD_MODEL_PATHS}")
    logger.info(f"  AITuber   : {HA_CHAR_BRIDGE_WS_URL}")
    logger.info(f"  Mic index : {MIC_DEVICE_INDEX}")
    logger.info("Wake word 待受中... Ctrl+C で終了")
    logger.info("=" * 60)

    await runner.run(task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[exit] stopped")
