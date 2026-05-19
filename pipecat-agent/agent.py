"""
Pipecat-based voice AI agent for V_agent.  v3 — Pipecat 1.2.1 対応.

Target: wake word 後 → AITuber Kit 発話開始まで <800ms

Pipeline:
  LocalAudioTransport (mic)
    → VADProcessor        (SileroVAD: start_secs=0.2, stop_secs=0.2)
    → WakeWordGate        (openWakeWord: "Hey Kemy")
    → [STT]
        openai  (推奨): OpenAISTTService gpt-4o-mini-transcribe  CER=1.2%
        deepgram(高速): DeepgramSTTService streaming ~250ms       CER=65%
    → context aggregator   (user turn, user_turn_stop_timeout=0.3)
    → [LLM]
        groq:   GroqLLMService    llama-3.3-70b TTFT ~150ms  HA精度100%
        openai: OpenAILLMService  gpt-4o-mini   TTFT ~640ms  HA精度100%
    → AITuberSink          (→ ha-character-bridge WS → AITuber Kit → VOICEVOX)
    → context aggregator   (assistant turn)

推定 end-to-end:
  openai-stt + groq-70b: 0.88 + 0.15 ≈ 1.03s  (精度優先・推奨)
  deepgram   + groq-70b: 0.25 + 0.15 ≈ 0.40s  (速度優先・日本語CER 65%注意)
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
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContext,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from ha_tools import HA_TOOLS_SCHEMA, run_ha_action
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
# bench_accuracy.py 結果: Deepgram 日本語CER=57-65% vs OpenAI CER=1.2%
# → 日本語精度は OpenAI が大幅に優位。速度重視なら Deepgram だが要注意。
STT_BACKEND = os.getenv("STT_BACKEND", "openai").lower()     # openai | deepgram
LLM_BACKEND = os.getenv("LLM_BACKEND", "groq").lower()      # groq | openai

# --- Model names ---
# bench_accuracy.py 結果: gpt-4o-mini と gpt-4o-transcribe は同精度 (CER=1.2%)
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-mini-transcribe")  # OpenAI STT only
# bench_accuracy.py 結果: gpt-4o-mini が gpt-4o を上回る (100% vs 67% HA精度)
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
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
「電気つけて」「電気消して」のように場所が省略された場合は洗面所を指すと解釈して操作してください。
返答は必ず1〜2文で簡潔にしてください。"""


# ---------------------------------------------------------------------------
# Service builders
# ---------------------------------------------------------------------------


def _build_stt():
    """Return the configured STT service."""
    if STT_BACKEND == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService  # noqa: PLC0415

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

    # default: OpenAI batch
    from pipecat.services.openai.stt import OpenAISTTService  # noqa: PLC0415

    logger.info(f"[stt] OpenAI batch  model={STT_MODEL}")
    return OpenAISTTService(
        api_key=OPENAI_API_KEY,
        settings=OpenAISTTService.Settings(model=STT_MODEL, language="ja"),
    )


def _build_llm():
    """Return the configured LLM service."""
    if LLM_BACKEND == "groq":
        from pipecat.services.groq.llm import GroqLLMService  # noqa: PLC0415

        logger.info(f"[llm] Groq  model={GROQ_MODEL}")
        return GroqLLMService(
            api_key=GROQ_API_KEY,
            settings=GroqLLMService.Settings(model=GROQ_MODEL),
        )

    from pipecat.services.openai.llm import OpenAILLMService  # noqa: PLC0415

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

    # --- Transport: mic input only (VAD is a separate pipeline processor in 1.2.1) ---
    transport = LocalAudioTransport(
        params=LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_passthrough=True,
            audio_out_enabled=False,
            input_device_index=MIC_DEVICE_INDEX,
        )
    )

    # --- VAD processor (separate pipeline stage in Pipecat 1.2.1) ---
    # stop_secs=0.2: 旧 voice-listener の END_SILENCE_SECONDS=0.9 から 0.7s 短縮
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                start_secs=VAD_START_SECS,
                stop_secs=VAD_STOP_SECS,
                confidence=VAD_CONFIDENCE,
            )
        )
    )

    # --- STT / LLM ---
    stt = _build_stt()
    llm = _build_llm()

    context = LLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=HA_TOOLS_SCHEMA,
    )

    # user_turn_stop_timeout=0.3: デフォルト 5.0s を 4.7s 短縮
    context_pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(user_turn_stop_timeout=0.3),
    )

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
    pipeline = Pipeline(
        [
            transport.input(),
            vad,             # SileroVAD → VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame
            wake_gate,       # wake word gate (VAD frames pass through only after wake word)
            stt,
            context_pair.user(),
            llm,
            aituber_sink,
            context_pair.assistant(),
        ]
    )

    task = PipelineTask(pipeline)
    runner = PipelineRunner()

    est_stt = "~250ms (streaming)" if STT_BACKEND == "deepgram" else "~880ms (batch)"
    est_llm = "~150ms TTFT" if LLM_BACKEND == "groq" else "~640ms TTFT"

    logger.info("=" * 64)
    logger.info("Pipecat Voice Agent v3 — Pipecat 1.2.1")
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
