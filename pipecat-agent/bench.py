#!/usr/bin/env python3
"""
Latency benchmark: pipecat-agent vs old openclaw+voice-listener system.

Tests (each N=5 samples):
  [A] STT latency         : whisper-1 / gpt-4o-mini-transcribe / gpt-4o-transcribe
  [B] LLM TTFT            : gpt-4o / gpt-4o-mini  x  casual / HA tool call
  [C] LLM full response   : same combinations
  [D] ha-bridge HTTP      : /actions round-trip (Docker サービスが必要)
  [E] ha-character-bridge : WebSocket connect+send round-trip

Summary: old system vs pipecat の推定 end-to-end レイテンシを比較

Usage:
    .venv/bin/python bench.py [--n 5] [--wav /path/to/speech.wav]
"""

import argparse
import asyncio
import io
import json
import os
import statistics
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------
# Setup: load API key from pipecat-agent .env → fallback voice-listener .env
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
if not os.getenv("OPENAI_API_KEY"):
    load_dotenv(APP_DIR / "../voice-listener/.env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
HA_BRIDGE_URL = os.getenv("HA_BRIDGE_URL", "http://127.0.0.1:18088")
HA_CHAR_BRIDGE_WS_URL = os.getenv("HA_CHAR_BRIDGE_WS_URL", "ws://127.0.0.1:8000/ws")
SAMPLE_RATE = 16000

HA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_ha_action",
            "description": "Home Assistantのデバイスを操作する",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["bathroom_light_on", "bathroom_light_off"],
                    }
                },
                "required": ["action"],
            },
        },
    }
]


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


def generate_speech_like_wav(duration_sec: float = 2.0) -> bytes:
    """
    Synthetic audio that mimics Japanese speech length/energy.
    Mixes formant-range frequencies with syllable-rhythm AM modulation.
    STT won't transcribe it as real speech, but measures API round-trip latency.
    """
    n = int(SAMPLE_RATE * duration_sec)
    t = np.linspace(0, duration_sec, n, dtype=np.float32)
    audio = (
        0.35 * np.sin(2 * np.pi * 250 * t)
        + 0.30 * np.sin(2 * np.pi * 800 * t)
        + 0.20 * np.sin(2 * np.pi * 2000 * t)
        + 0.15 * np.sin(2 * np.pi * 3500 * t)
    )
    mod = 0.5 + 0.5 * np.sin(2 * np.pi * 4.5 * t)  # ~4.5 syllables/sec rhythm
    audio = (audio * mod * 0.7).astype(np.float32)
    pcm16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


def load_wav(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    label: str
    samples: list[float] = field(default_factory=list)

    def add(self, v: float) -> None:
        self.samples.append(v)

    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def mean(self) -> float:
        return statistics.mean(self.samples) if self.samples else float("nan")

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else float("nan")

    @property
    def p90(self) -> float:
        if not self.samples:
            return float("nan")
        s = sorted(self.samples)
        return s[min(int(len(s) * 0.9), len(s) - 1)]

    @property
    def minimum(self) -> float:
        return min(self.samples) if self.samples else float("nan")

    @property
    def maximum(self) -> float:
        return max(self.samples) if self.samples else float("nan")

    def row(self) -> str:
        if not self.samples:
            return f"  {self.label:<40} N/A"
        return (
            f"  {self.label:<40} "
            f"mean={self.mean:.3f}s  "
            f"med={self.median:.3f}s  "
            f"p90={self.p90:.3f}s  "
            f"min={self.minimum:.3f}s  "
            f"max={self.maximum:.3f}s  "
            f"(n={self.n})"
        )


# ---------------------------------------------------------------------------
# [A] STT latency
# ---------------------------------------------------------------------------


def run_stt_bench(wav_bytes: bytes, model: str, n: int) -> Stats:
    client = OpenAI(api_key=OPENAI_API_KEY)
    stats = Stats(label=model)
    print(f"  {model} ", end="", flush=True)
    for _ in range(n):
        buf = io.BytesIO(wav_bytes)
        buf.name = "bench.wav"
        t0 = time.perf_counter()
        try:
            client.audio.transcriptions.create(
                model=model,
                file=buf,
                language="ja",
                temperature=0,
            )
        except Exception as e:
            print(f"\n    error: {e}", file=sys.stderr)
        elapsed = time.perf_counter() - t0
        stats.add(elapsed)
        print(".", end="", flush=True)
        time.sleep(0.4)
    print()
    return stats


# ---------------------------------------------------------------------------
# [B/C] LLM latency (TTFT + full)
# ---------------------------------------------------------------------------


def run_llm_bench(
    prompt: str,
    label: str,
    model: str,
    use_tools: bool,
    n: int,
) -> tuple[Stats, Stats]:
    client = OpenAI(api_key=OPENAI_API_KEY)
    ttft_stats = Stats(label=f"{label} [{model}] TTFT")
    full_stats = Stats(label=f"{label} [{model}] full")
    messages = [
        {
            "role": "system",
            "content": "あなたは家のAIキャラクターです。日本語で簡潔に答えてください。",
        },
        {"role": "user", "content": prompt},
    ]
    kwargs: dict = {"model": model, "messages": messages, "stream": True}
    if use_tools:
        kwargs["tools"] = HA_TOOLS
        kwargs["tool_choice"] = "auto"

    print(f"  {label} [{model}] ", end="", flush=True)
    for _ in range(n):
        t0 = time.perf_counter()
        ttft_recorded = False
        try:
            stream = client.chat.completions.create(**kwargs)
            for chunk in stream:
                if not ttft_recorded:
                    ttft_stats.add(time.perf_counter() - t0)
                    ttft_recorded = True
        except Exception as e:
            print(f"\n    error: {e}", file=sys.stderr)
        full_stats.add(time.perf_counter() - t0)
        print(".", end="", flush=True)
        time.sleep(0.6)
    print()
    return ttft_stats, full_stats


# ---------------------------------------------------------------------------
# [D] ha-bridge HTTP
# ---------------------------------------------------------------------------


def run_ha_bridge_bench(n: int) -> Stats | None:
    stats = Stats(label="ha-bridge /actions HTTP")
    print("  ha-bridge ", end="", flush=True)
    ok = 0
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            requests.get(f"{HA_BRIDGE_URL}/actions", timeout=3)
            ok += 1
        except Exception:
            print("x", end="", flush=True)
            stats.add(time.perf_counter() - t0)
            time.sleep(0.1)
            continue
        stats.add(time.perf_counter() - t0)
        print(".", end="", flush=True)
        time.sleep(0.1)
    print()
    return stats if ok > 0 else None


# ---------------------------------------------------------------------------
# [E] ha-character-bridge WebSocket
# ---------------------------------------------------------------------------


async def _ws_ping(url: str) -> float | None:
    try:
        import websockets  # noqa: PLC0415
    except ImportError:
        return None
    payload = json.dumps({"text": "ベンチマークテスト", "emotion": "neutral"}, ensure_ascii=False)
    t0 = time.perf_counter()
    try:
        async with websockets.connect(url, open_timeout=3) as ws:
            await ws.send(payload)
        return time.perf_counter() - t0
    except Exception:
        return None


async def run_ws_bench(n: int) -> Stats | None:
    stats = Stats(label="ha-character-bridge WebSocket")
    print("  ha-character-bridge WS ", end="", flush=True)
    for _ in range(n):
        v = await _ws_ping(HA_CHAR_BRIDGE_WS_URL)
        if v is not None:
            stats.add(v)
            print(".", end="", flush=True)
        else:
            print("x", end="", flush=True)
        await asyncio.sleep(0.1)
    print()
    return stats if stats.samples else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SEPARATOR = "=" * 72


def section(title: str) -> None:
    print(f"\n{SEPARATOR}")
    print(f"  {title}")
    print(SEPARATOR)


async def main(n: int, wav_path: str | None) -> None:
    if not OPENAI_API_KEY:
        sys.exit("ERROR: OPENAI_API_KEY が設定されていません (.env または voice-listener/.env)")

    print(SEPARATOR)
    print("  Latency Benchmark — pipecat-agent vs old system")
    print(f"  N={n} samples per pattern  |  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(SEPARATOR)

    # --- Audio setup ---
    if wav_path:
        wav_bytes = load_wav(wav_path)
        import wave as _wave
        with _wave.open(wav_path) as _w:
            dur = _w.getnframes() / _w.getframerate()
        print(f"\n[audio] real WAV: {wav_path} ({dur:.1f}s)")
    else:
        wav_bytes_1s = generate_speech_like_wav(1.0)
        wav_bytes_2s = generate_speech_like_wav(2.0)
        wav_bytes_3s = generate_speech_like_wav(3.0)
        print("\n[audio] synthetic WAV (1s / 2s / 3s) を生成しました")
        print("        ※実際の日本語音声ではないため転写精度の評価には使えませんが、")
        print("          API round-trip レイテンシは計測できます。")

    # -----------------------------------------------------------------------
    # [A] STT
    # -----------------------------------------------------------------------
    section("[A] STT Latency  (音声 → 文字起こし API 往復)")

    stt_results: dict[str, dict] = {}
    for dur_label, dur_bytes in [
        ("1s", wav_bytes_1s if not wav_path else wav_bytes),
        ("2s", wav_bytes_2s if not wav_path else wav_bytes),
        ("3s", wav_bytes_3s if not wav_path else wav_bytes),
    ]:
        if wav_path and dur_label != "2s":
            continue  # real wav: test once
        print(f"\n  --- 音声長 {dur_label} ---")
        for model in ["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"]:
            s = run_stt_bench(dur_bytes, model, n)
            stt_results.setdefault(dur_label, {})[model] = s

    print()
    for dur_label, models in stt_results.items():
        print(f"  ── 音声長 {dur_label} ──")
        for s in models.values():
            print(s.row())

    # -----------------------------------------------------------------------
    # [B/C] LLM
    # -----------------------------------------------------------------------
    section("[B/C] LLM Latency  (TTFT = time to first token)")

    llm_cases = [
        ("casual_short", "こんにちは！今日は何をする？", False),
        ("casual_long", "最近のおすすめ映画を教えてください。", False),
        ("ha_tool_call", "洗面所の電気をつけて", True),
    ]
    llm_models = ["gpt-4o-mini", "gpt-4o"]

    all_llm: list[tuple[Stats, Stats]] = []
    for prompt_label, prompt, use_tools in llm_cases:
        print(f"\n  ── {prompt_label}: 「{prompt}」 ──")
        for model in llm_models:
            pair = run_llm_bench(prompt, prompt_label, model, use_tools, n)
            all_llm.append(pair)

    print("\n  TTFT (ストリーム最初のチャンク受信まで)")
    for ttft, _ in all_llm:
        print(ttft.row())
    print("\n  Full response (最終チャンク受信まで)")
    for _, full in all_llm:
        print(full.row())

    # -----------------------------------------------------------------------
    # [D] ha-bridge HTTP
    # -----------------------------------------------------------------------
    section("[D] ha-bridge HTTP  (localhost:18088 — Docker 起動時のみ)")
    ha_stats = run_ha_bridge_bench(n)
    if ha_stats:
        print(ha_stats.row())
    else:
        print("  ha-bridge 未起動のためスキップ (0ms として推定に使用)")

    # -----------------------------------------------------------------------
    # [E] ha-character-bridge WebSocket
    # -----------------------------------------------------------------------
    section("[E] ha-character-bridge WebSocket  (localhost:8000 — bridge 起動時のみ)")
    ws_stats = await run_ws_bench(n)
    if ws_stats:
        print(ws_stats.row())
    else:
        print("  ha-character-bridge 未起動のためスキップ (5ms として推定に使用)")

    # -----------------------------------------------------------------------
    # Summary: pipeline estimate
    # -----------------------------------------------------------------------
    section("SUMMARY — End-to-End レイテンシ推定 (wake word 後 → AITuber 発話開始まで)")

    # pick representative measured values
    stt_ref = stt_results.get("2s", stt_results.get("1s", {}))
    stt_new = stt_ref.get("gpt-4o-transcribe")
    stt_old = stt_new  # same model
    stt_mean_new = stt_new.mean if stt_new and stt_new.samples else 1.2

    # LLM: gpt-4o casual TTFT / full
    llm_4o_casual_ttft: list[float] = []
    llm_4o_casual_full: list[float] = []
    llm_4o_tool_full: list[float] = []
    for (ttft, full) in all_llm:
        if "casual_short" in ttft.label and "gpt-4o]" in ttft.label and "mini" not in ttft.label:
            llm_4o_casual_ttft = ttft.samples
            llm_4o_casual_full = full.samples
        if "ha_tool_call" in full.label and "gpt-4o]" in full.label and "mini" not in full.label:
            llm_4o_tool_full = full.samples

    llm_ttft = statistics.mean(llm_4o_casual_ttft) if llm_4o_casual_ttft else 0.5
    llm_full_casual = statistics.mean(llm_4o_casual_full) if llm_4o_casual_full else 1.2
    llm_full_tool = statistics.mean(llm_4o_tool_full) if llm_4o_tool_full else 1.8

    ha_http_ms = ha_stats.mean if ha_stats and ha_stats.samples else 0.005
    ws_ms = ws_stats.mean if ws_stats and ws_stats.samples else 0.005

    # Old system: END_SILENCE(0.9) + STT + OpenClaw gateway overhead + LLM + bridges
    OLD_SILENCE_WAIT = 0.9   # END_SILENCE_SECONDS from voice-listener/.env
    OLD_GATEWAY_OH = 0.15    # interaction-bridge → openclaw gateway HTTP overhead
    old_casual = OLD_SILENCE_WAIT + stt_mean_new + OLD_GATEWAY_OH + llm_full_casual + ha_http_ms + ws_ms
    old_tool   = OLD_SILENCE_WAIT + stt_mean_new + OLD_GATEWAY_OH + llm_full_tool   + ha_http_ms + ws_ms

    # Pipecat: VAD instant + STT + LLM TTFT (streaming starts immediately) + WS
    PIPECAT_VAD_OVERHEAD = 0.05  # Silero VAD detection margin
    pipecat_casual = PIPECAT_VAD_OVERHEAD + stt_mean_new + llm_ttft + ws_ms
    pipecat_tool   = PIPECAT_VAD_OVERHEAD + stt_mean_new + llm_full_tool + ws_ms

    def bar(v: float, scale: float = 20.0) -> str:
        filled = max(1, int(v * scale))
        return "█" * filled

    print(f"""
  ┌─────────────────────────────────────────────────────────────────────┐
  │ 旧システム (openclaw + voice-listener)   END_SILENCE={OLD_SILENCE_WAIT:.1f}s          │
  ├─────────────────────────────────────────────────────────────────────┤
  │ casual:  {old_casual:.2f}s  {bar(old_casual)}  │
  │ HA操作:  {old_tool:.2f}s  {bar(old_tool)}  │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 内訳: {OLD_SILENCE_WAIT:.2f}s 無音待ち  +  {stt_mean_new:.2f}s STT  +  {OLD_GATEWAY_OH:.2f}s gateway OH  +  LLM  +  WS │
  └─────────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────────┐
  │ Pipecat (pipecat-agent)                                              │
  ├─────────────────────────────────────────────────────────────────────┤
  │ casual:  {pipecat_casual:.2f}s  {bar(pipecat_casual)}  │
  │ HA操作:  {pipecat_tool:.2f}s  {bar(pipecat_tool)}  │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 内訳: {PIPECAT_VAD_OVERHEAD:.2f}s VAD  +  {stt_mean_new:.2f}s STT  +  TTFT={llm_ttft:.2f}s  +  {ws_ms:.3f}s WS   │
  └─────────────────────────────────────────────────────────────────────┘

  改善量 (casual):  -{(old_casual - pipecat_casual):.2f}s  ({(1 - pipecat_casual/old_casual)*100:.0f}% 削減)
  改善量 (HA操作):  -{(old_tool   - pipecat_tool  ):.2f}s  ({(1 - pipecat_tool  /old_tool  )*100:.0f}% 削減)

  ※ 最大ボトルネックは STT ({stt_mean_new:.2f}s) と LLM ({llm_ttft:.2f}s TTFT)
  ※ さらに下げるには Deepgram STT (streaming, ~0.3s) + Claude Haiku or gpt-4o-mini を検討
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Latency benchmark for pipecat-agent")
    parser.add_argument("--n", type=int, default=5, help="samples per test (default 5)")
    parser.add_argument("--wav", type=str, default=None, help="path to real WAV file for STT test")
    args = parser.parse_args()
    asyncio.run(main(n=args.n, wav_path=args.wav))
