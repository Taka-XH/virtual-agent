"""
Pipecat-based voice AI agent for V_agent.  v2 — latency-optimized.

Target: wake word 後 → AITuber Kit 発話開始まで <800ms

Pipeline:
  LocalAudioTransport (mic + Silero VAD, stop_secs=0.2)
    → WakeWordGate         (openWakeWord: "Hey Kemy")
    → [STT]
        deepgram (推奨): DeepgramSTTService  streaming ~250ms
        openai  (fallback): OpenAISTTService batch    ~880ms
    → context aggregator   (user turn, aggregation_timeout=0.3)
    → [LLM]
        openai: OpenAILLMService  gpt-4o   TTFT ~640ms
        groq:   GroqLLMService    llama-70b TTFT ~150ms (日本語品質は劣る)
    → AITuberSink          (→ ha-character-bridge WS → AITuber Kit → VOICEVOX)
    → context aggregator   (assistant turn)

推定 end-to-end:
  deepgram + gpt-4o   : 0.05 + 0.25 + 0.64 ≈ 0.94s
  deepgram + groq-70b : 0.05 + 0.25 + 0.15 ≈ 0.45s
  openai   + gpt-4o   : 0.05 + 0.88 + 0.64 ≈ 1.57s (旧 pipecat 構成)
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
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
    paths = []
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        resolved = (APP_DIR / p).resolve() if not Path(p).is_absolute() else Path(p)
        paths.append(str(resolved))
    return paths


# --- API keys ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# --- Backend selection ---
STT_BACKEND = os.getenv("STT_BACKEND", "deepgram").lower()   # deepgram | openai
LLM_BACKEND = os.getenv("LLM_BACKEND", "openai").lower()    # openai | groq

# --- Model names ---
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-transcribe")      # OpenAI STT only
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# --- Deepgram settings ---
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-3-general")
DEEPGRAM_ENDPOINTING_MS = int(os.getenv("DEEPGRAM_ENDPOINTING_MS", "300"))
DEEPGRAM_UTTERANCE_END_MS = int(os.getenv("DEEPGRAM_UTTERANCE_END_MS", "1000"))

# --- VAD settings ---
VAD_START_SECS = float(os.getenv("VAD_START_SECS", "0.2"))
VAD_STOP_SECS = float(os.getenv("VAD_STOP_SECS", "0.2"))
VAD_CONFIDENCE = float(os.getenv("VAD_CONFIDENCE", "0.7"))

# --- Misc ---
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
返答は必ず1〜2文で簡潔にしてください。"""


# ---------------------------------------------------------------------------
# Service builders
# ---------------------------------------------------------------------------


def _build_stt():
    """Return the configured STT service."""
    if STT_BACKEND == "deepgram":
        from pipecat.services.deepgram import DeepgramSTTService  # noqa: PLC0415

        logger.info(f"[stt] Deepgram streaming  model={DEEPGRAM_MODEL}  endpointing={DEEPGRAM_ENDPOINTING_MS}ms")
        return DeepgramSTTService(
            api_key=DEEPGRAM_API_KEY,
            settings=DeepgramSTTService.Settings(
                model=DEEPGRAM_MODEL,
                language="ja",
                endpointing=DEEPGRAM_ENDPOINTING_MS,
                utterance_end_ms=DEEPGRAM_UTTERANCE_END_MS,
                interim_results=True,
                smart_format=True,
                punctuate=True,
            ),
        )

    # fallback: OpenAI batch
    from pipecat.services.openai import OpenAISTTService  # noqa: PLC0415

    logger.info(f"[stt] OpenAI batch  model={STT_MODEL}")
    return OpenAISTTService(
        api_key=OPENAI_API_KEY,
        model=STT_MODEL,
        language="ja",
    )


def _build_llm():
    """Return the configured LLM service."""
    if LLM_BACKEND == "groq":
        from pipecat.services.groq import GroqLLMService  # noqa: PLC0415

        logger.info(f"[llm] Groq  model={GROQ_MODEL}")
        return GroqLLMService(
            api_key=GROQ_API_KEY,
            model=GROQ_MODEL,
        )

    from pipecat.services.openai import OpenAILLMService  # noqa: PLC0415

    logger.info(f"[llm] OpenAI  model={LLM_MODEL}")
    return OpenAILLMService(
        api_key=OPENAI_API_KEY,
        model=LLM_MODEL,
        params=OpenAILLMService.InputParams(
            max_tokens=200,   # 短い応答で LLM 完了時間を削減
        ),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    # --- Validate keys ---
    if STT_BACKEND == "deepgram" and not DEEPGRAM_API_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY が .env に設定されていません (STT_BACKEND=deepgram)")
    if STT_BACKEND == "openai" and not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY が .env に設定されていません (STT_BACKEND=openai)")
    if LLM_BACKEND == "groq" and not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY が .env に設定されていません (LLM_BACKEND=groq)")
    if LLM_BACKEND == "openai" and not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY が .env に設定されていません (LLM_BACKEND=openai)")

    # --- Transport: mic + Silero VAD ---
    # stop_secs=0.2: 旧 voice-listener の END_SILENCE_SECONDS=0.9 から 0.7s 短縮
    transport = LocalAudioTransport(
        params=LocalAudioParams(
            audio_in_enabled=True,
            audio_out_enabled=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    start_secs=VAD_START_SECS,
                    stop_secs=VAD_STOP_SECS,
                    confidence=VAD_CONFIDENCE,
                )
            ),
            vad_audio_passthrough=True,
            input_device_index=MIC_DEVICE_INDEX,
        )
    )

    # --- STT / LLM ---
    stt = _build_stt()
    llm = _build_llm()

    context = OpenAILLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=HA_TOOL_DEFINITIONS,
    )

    # aggregation_timeout=0.3: デフォルト 1.0s (Issue #1319) を 0.7s 短縮
    try:
        context_aggregator = llm.create_context_aggregator(context, aggregation_timeout=0.3)
    except TypeError:
        # 古いバージョンでは引数なし
        context_aggregator = llm.create_context_aggregator(context)

    # --- HA function call handler ---
    async def handle_run_ha_action(
        function_name: str,
        tool_call_id: str,
        arguments: dict,
        llm,       # noqa: ARG001
        context,   # noqa: ARG001
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
    # 割り込み (bot 発話中にユーザーが話し始めたとき) は Pipecat がデフォルトで処理する。
    # UserStartedSpeakingFrame が来ると bot の生成をキャンセルし新しいターンを開始。
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

    est_stt = "~250ms (streaming)" if STT_BACKEND == "deepgram" else "~880ms (batch)"
    est_llm = "~150ms TTFT" if LLM_BACKEND == "groq" else "~640ms TTFT"

    logger.info("=" * 64)
    logger.info("Pipecat Voice Agent v2 — latency-optimized")
    logger.info(f"  STT : {STT_BACKEND}  {est_stt}")
    logger.info(f"  LLM : {LLM_BACKEND}  {est_llm}")
    logger.info(f"  VAD : stop_secs={VAD_STOP_SECS}s  confidence={VAD_CONFIDENCE}")
    logger.info(f"  Wake: threshold={WAKE_THRESHOLD}  confirm={WAKE_CONFIRM_CHUNKS}")
    logger.info(f"  Out : {HA_CHAR_BRIDGE_WS_URL}")
    logger.info("Wake word 待受中... Ctrl+C で終了")
    logger.info("=" * 64)

    await runner.run(task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[exit] stopped")
