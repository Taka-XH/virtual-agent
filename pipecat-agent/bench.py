#!/usr/bin/env python3
"""
Latency benchmark: pipecat-agent v2 — full stack comparison.

Tests (N samples each):
  [A] STT batch     : whisper-1 / gpt-4o-mini-transcribe / gpt-4o-transcribe (OpenAI)
  [A2] STT Deepgram : nova-2-general / nova-3-general  (pre-recorded API)
  [B] LLM OpenAI    : gpt-4o-mini / gpt-4o  x  casual / HA tool call  (TTFT + full)
  [B2] LLM Groq     : llama-3.1-8b-instant / llama-3.3-70b-versatile  (TTFT + full)
  [C] ha-bridge HTTP
  [D] ha-character-bridge WebSocket

Summary: old system / pipecat v1 / pipecat v2 (Deepgram+Groq) の推定 e2e レイテンシ比較

Usage:
    .venv/bin/python bench.py [--n 3] [--wav /path/to/speech.wav] [--skip-groq] [--skip-deepgram]
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

import httpx
import numpy as np
import requests
from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
if not os.getenv("OPENAI_API_KEY"):
    load_dotenv(APP_DIR / "../voice-listener/.env")

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")

HA_BRIDGE_URL        = os.getenv("HA_BRIDGE_URL", "http://127.0.0.1:18088")
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
                    "action": {"type": "string", "enum": ["bathroom_light_on", "bathroom_light_off"]},
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
    """Synthetic speech-like audio (formant mix + AM rhythm)."""
    n = int(SAMPLE_RATE * duration_sec)
    t = np.linspace(0, duration_sec, n, dtype=np.float32)
    audio = (
        0.35 * np.sin(2 * np.pi * 250 * t)
        + 0.30 * np.sin(2 * np.pi * 800 * t)
        + 0.20 * np.sin(2 * np.pi * 2000 * t)
        + 0.15 * np.sin(2 * np.pi * 3500 * t)
    )
    mod = 0.5 + 0.5 * np.sin(2 * np.pi * 4.5 * t)
    pcm16 = np.clip(audio * mod * 0.7 * 32767, -32768, 32767).astype(np.int16)
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

    def row(self, width: int = 42) -> str:
        if not self.samples:
            return f"  {self.label:<{width}} N/A"
        return (
            f"  {self.label:<{width}} "
            f"mean={self.mean:.3f}s  "
            f"med={self.median:.3f}s  "
            f"p90={self.p90:.3f}s  "
            f"min={self.minimum:.3f}s  "
            f"(n={self.n})"
        )


# ---------------------------------------------------------------------------
# [A] STT — OpenAI batch
# ---------------------------------------------------------------------------

def run_openai_stt(wav_bytes: bytes, model: str, n: int) -> Stats:
    client = OpenAI(api_key=OPENAI_API_KEY)
    stats = Stats(label=f"OpenAI/{model}")
    print(f"  {model} ", end="", flush=True)
    for _ in range(n):
        buf = io.BytesIO(wav_bytes)
        buf.name = "bench.wav"
        t0 = time.perf_counter()
        try:
            client.audio.transcriptions.create(model=model, file=buf, language="ja", temperature=0)
        except Exception as e:
            print(f"\n    error: {e}", file=sys.stderr)
        stats.add(time.perf_counter() - t0)
        print(".", end="", flush=True)
        time.sleep(0.4)
    print()
    return stats


# ---------------------------------------------------------------------------
# [A2] STT — Deepgram pre-recorded (batch API for fair round-trip comparison)
# ---------------------------------------------------------------------------

async def _deepgram_once(client: httpx.AsyncClient, wav_bytes: bytes, model: str) -> float:
    t0 = time.perf_counter()
    await client.post(
        "https://api.deepgram.com/v1/listen",
        params={"model": model, "language": "ja", "punctuate": "true",
                "smart_format": "true", "endpointing": "300"},
        headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/wav"},
        content=wav_bytes,
        timeout=30,
    )
    return time.perf_counter() - t0


async def run_deepgram_stt(wav_bytes: bytes, model: str, n: int) -> Stats:
    stats = Stats(label=f"Deepgram/{model}")
    print(f"  {model} ", end="", flush=True)
    async with httpx.AsyncClient() as client:
        for _ in range(n):
            try:
                elapsed = await _deepgram_once(client, wav_bytes, model)
                stats.add(elapsed)
                print(".", end="", flush=True)
            except Exception as e:
                print(f"\n    error: {e}", file=sys.stderr)
            await asyncio.sleep(0.3)
    print()
    return stats


# ---------------------------------------------------------------------------
# [B] LLM — OpenAI  (TTFT + full)
# ---------------------------------------------------------------------------

def run_openai_llm(prompt: str, label: str, model: str, use_tools: bool, n: int) -> tuple[Stats, Stats]:
    client = OpenAI(api_key=OPENAI_API_KEY)
    ttft_s = Stats(label=f"{label} [{model}] TTFT")
    full_s = Stats(label=f"{label} [{model}] full")
    msgs = [
        {"role": "system", "content": "あなたは家のAIキャラクターです。日本語で1〜2文で答えてください。"},
        {"role": "user", "content": prompt},
    ]
    kwargs: dict = {"model": model, "messages": msgs, "stream": True, "max_tokens": 200}
    if use_tools:
        kwargs["tools"] = HA_TOOLS
        kwargs["tool_choice"] = "auto"

    print(f"  {label} [{model}] ", end="", flush=True)
    for _ in range(n):
        t0 = time.perf_counter()
        ttft_done = False
        try:
            for chunk in client.chat.completions.create(**kwargs):
                if not ttft_done:
                    ttft_s.add(time.perf_counter() - t0)
                    ttft_done = True
        except Exception as e:
            print(f"\n    error: {e}", file=sys.stderr)
        full_s.add(time.perf_counter() - t0)
        print(".", end="", flush=True)
        time.sleep(0.6)
    print()
    return ttft_s, full_s


# ---------------------------------------------------------------------------
# [B2] LLM — Groq  (TTFT + full)
# ---------------------------------------------------------------------------

async def run_groq_llm(prompt: str, label: str, model: str, use_tools: bool, n: int) -> tuple[Stats, Stats]:
    ttft_s = Stats(label=f"{label} [groq/{model}] TTFT")
    full_s = Stats(label=f"{label} [groq/{model}] full")
    msgs = [
        {"role": "system", "content": "あなたは家のAIキャラクターです。日本語で1〜2文で答えてください。"},
        {"role": "user", "content": prompt},
    ]
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    body: dict = {"model": model, "messages": msgs, "stream": True, "max_tokens": 200}
    if use_tools:
        body["tools"] = HA_TOOLS
        body["tool_choice"] = "auto"

    print(f"  {label} [groq/{model}] ", end="", flush=True)
    async with httpx.AsyncClient() as client:
        for _ in range(n):
            t0 = time.perf_counter()
            ttft_done = False
            try:
                async with client.stream(
                    "POST",
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json=body,
                    timeout=30,
                ) as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            if not ttft_done:
                                ttft_s.add(time.perf_counter() - t0)
                                ttft_done = True
            except Exception as e:
                print(f"\n    error: {e}", file=sys.stderr)
            full_s.add(time.perf_counter() - t0)
            print(".", end="", flush=True)
            await asyncio.sleep(0.5)
    print()
    return ttft_s, full_s


# ---------------------------------------------------------------------------
# [C] ha-bridge HTTP
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
            time.sleep(0.1)
            continue
        stats.add(time.perf_counter() - t0)
        print(".", end="", flush=True)
        time.sleep(0.1)
    print()
    return stats if ok > 0 else None


# ---------------------------------------------------------------------------
# [D] ha-character-bridge WebSocket
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
    stats = Stats(label="ha-character-bridge WS")
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
# Helpers
# ---------------------------------------------------------------------------

SEP = "=" * 72

def section(title: str) -> None:
    print(f"\n{SEP}\n  {title}\n{SEP}")

def bar(v: float, scale: float = 18.0) -> str:
    return "█" * max(1, int(v * scale))

def _mean(samples: list[float], default: float) -> float:
    return statistics.mean(samples) if samples else default


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(n: int, wav_path: str | None, skip_groq: bool, skip_deepgram: bool) -> None:
    if not OPENAI_API_KEY:
        sys.exit("ERROR: OPENAI_API_KEY が未設定です")

    print(SEP)
    print("  Latency Benchmark v2 — pipecat-agent (Deepgram + Groq)")
    print(f"  N={n} samples  |  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Deepgram: {'あり' if DEEPGRAM_API_KEY and not skip_deepgram else 'スキップ'}  "
          f"Groq: {'あり' if GROQ_API_KEY and not skip_groq else 'スキップ'}")
    print(SEP)

    # --- Audio ---
    if wav_path:
        wav_bytes = load_wav(wav_path)
        import wave as _w
        with _w.open(wav_path) as wf:
            dur = wf.getnframes() / wf.getframerate()
        print(f"\n[audio] real WAV: {wav_path}  ({dur:.1f}s)")
        wav_2s = wav_bytes
    else:
        wav_2s = generate_speech_like_wav(2.0)
        print("\n[audio] synthetic 2s WAV (API round-trip 計測用・転写精度は無関係)")

    # -----------------------------------------------------------------------
    # [A] OpenAI STT batch
    # -----------------------------------------------------------------------
    section("[A] STT Latency — OpenAI batch  (2s 音声)")
    openai_stt: dict[str, Stats] = {}
    for model in ["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"]:
        openai_stt[model] = run_openai_stt(wav_2s, model, n)
    print()
    for s in openai_stt.values():
        print(s.row())

    # -----------------------------------------------------------------------
    # [A2] Deepgram batch  (pre-recorded API = 上限値・実際の streaming はより速い)
    # -----------------------------------------------------------------------
    deepgram_stt: dict[str, Stats] = {}
    if DEEPGRAM_API_KEY and not skip_deepgram:
        section("[A2] STT Latency — Deepgram pre-recorded API  (streaming 実測の上限値)")
        print("  ※ streaming は発話終了と同時に結果が来るため実際はさらに速い\n")
        for model in ["nova-2-general", "nova-3-general"]:
            deepgram_stt[model] = await run_deepgram_stt(wav_2s, model, n)
        print()
        for s in deepgram_stt.values():
            print(s.row())
    else:
        print(f"\n  [A2] Deepgram STT: スキップ (DEEPGRAM_API_KEY 未設定 or --skip-deepgram)")

    # -----------------------------------------------------------------------
    # [B] OpenAI LLM
    # -----------------------------------------------------------------------
    section("[B] LLM Latency — OpenAI  (TTFT = time to first token)")
    llm_cases = [
        ("casual", "こんにちは！今日は何をする？", False),
        ("ha_tool",  "洗面所の電気をつけて",        True),
    ]
    openai_llm: list[tuple[Stats, Stats]] = []
    for label, prompt, tools in llm_cases:
        print(f"\n  ── {label}: 「{prompt}」 ──")
        for model in ["gpt-4o-mini", "gpt-4o"]:
            openai_llm.append(run_openai_llm(prompt, label, model, tools, n))

    print("\n  TTFT")
    for ttft, _ in openai_llm:
        print(ttft.row())
    print("\n  Full response")
    for _, full in openai_llm:
        print(full.row())

    # -----------------------------------------------------------------------
    # [B2] Groq LLM
    # -----------------------------------------------------------------------
    groq_llm: list[tuple[Stats, Stats]] = []
    if GROQ_API_KEY and not skip_groq:
        section("[B2] LLM Latency — Groq  (超低 TTFT・日本語品質は gpt-4o より劣る)")
        for label, prompt, tools in llm_cases:
            print(f"\n  ── {label}: 「{prompt}」 ──")
            for model in ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]:
                groq_llm.append(await run_groq_llm(prompt, label, model, tools, n))

        print("\n  TTFT")
        for ttft, _ in groq_llm:
            print(ttft.row())
        print("\n  Full response")
        for _, full in groq_llm:
            print(full.row())
    else:
        print(f"\n  [B2] Groq LLM: スキップ (GROQ_API_KEY 未設定 or --skip-groq)")

    # -----------------------------------------------------------------------
    # [C] ha-bridge / [D] WS
    # -----------------------------------------------------------------------
    section("[C] ha-bridge HTTP  (Docker 起動時のみ)")
    ha_stats = run_ha_bridge_bench(n)
    print(ha_stats.row() if ha_stats else "  未起動 → 0ms として推定")

    section("[D] ha-character-bridge WebSocket  (bridge 起動時のみ)")
    ws_stats = await run_ws_bench(n)
    print(ws_stats.row() if ws_stats else "  未起動 → 5ms として推定")

    # -----------------------------------------------------------------------
    # SUMMARY
    # -----------------------------------------------------------------------
    section("SUMMARY — End-to-End 推定  (wake word 後 → AITuber Kit 発話テキスト到着まで)")

    # 代表値を取り出す
    stt_openai = _mean(openai_stt.get("gpt-4o-transcribe", Stats("")).samples, 0.88)
    stt_deepgram = _mean(
        next(iter(deepgram_stt.values()), Stats("")).samples, 0.30
    ) if deepgram_stt else 0.30

    # OpenAI LLM: gpt-4o casual TTFT
    gpt4o_casual_ttft: list[float] = []
    gpt4o_casual_full: list[float] = []
    gpt4o_tool_full:   list[float] = []
    for (ttft, full) in openai_llm:
        if "casual" in ttft.label and "gpt-4o]" in ttft.label and "mini" not in ttft.label:
            gpt4o_casual_ttft = ttft.samples
            gpt4o_casual_full = full.samples
        if "ha_tool" in full.label and "gpt-4o]" in full.label and "mini" not in full.label:
            gpt4o_tool_full = full.samples

    llm_ttft  = _mean(gpt4o_casual_ttft, 0.64)
    llm_full_c = _mean(gpt4o_casual_full, 1.19)
    llm_full_t = _mean(gpt4o_tool_full, 0.87)

    # Groq: llama-3.3-70b casual TTFT
    groq_casual_ttft: list[float] = []
    groq_tool_full:   list[float] = []
    for (ttft, full) in groq_llm:
        if "casual" in ttft.label and "70b" in ttft.label:
            groq_casual_ttft = ttft.samples
        if "ha_tool" in full.label and "70b" in full.label:
            groq_tool_full = full.samples
    groq_ttft   = _mean(groq_casual_ttft, 0.15)
    groq_full_t = _mean(groq_tool_full, 0.25)

    ws_ms = ws_stats.mean if ws_stats and ws_stats.samples else 0.005

    OLD_WAIT   = 0.9   # END_SILENCE_SECONDS
    OLD_GW_OH  = 0.15  # interaction-bridge → openclaw gateway
    VAD_OH     = 0.05  # Silero VAD margin

    configs = [
        ("旧システム (openclaw+voice-listener)",
         OLD_WAIT + stt_openai + OLD_GW_OH + llm_full_c,
         OLD_WAIT + stt_openai + OLD_GW_OH + llm_full_t,
         f"{OLD_WAIT:.2f}s 無音待ち + {stt_openai:.2f}s STT + {OLD_GW_OH:.2f}s gateway + LLM"),
        ("Pipecat v1 (OpenAI STT + gpt-4o)",
         VAD_OH + stt_openai + llm_ttft + ws_ms,
         VAD_OH + stt_openai + llm_full_t + ws_ms,
         f"{VAD_OH:.2f}s VAD + {stt_openai:.2f}s STT + {llm_ttft:.2f}s TTFT"),
        ("Pipecat v2 (Deepgram + gpt-4o)  ★推奨",
         VAD_OH + stt_deepgram + llm_ttft + ws_ms,
         VAD_OH + stt_deepgram + llm_full_t + ws_ms,
         f"{VAD_OH:.2f}s VAD + {stt_deepgram:.2f}s STT + {llm_ttft:.2f}s TTFT"),
        ("Pipecat v2 (Deepgram + Groq-70b)  ★最速",
         VAD_OH + stt_deepgram + groq_ttft + ws_ms,
         VAD_OH + stt_deepgram + groq_full_t + ws_ms,
         f"{VAD_OH:.2f}s VAD + {stt_deepgram:.2f}s STT + {groq_ttft:.2f}s TTFT"),
    ]

    TARGET = 0.8
    print()
    print(f"  目標: {TARGET:.1f}s 以内  (業界ベストプラクティス)")
    print()
    header = f"  {'構成':<42} casual    HA操作    目標達成"
    print(header)
    print("  " + "-" * 68)
    for name, casual, ha, breakdown in configs:
        ok_c = "✓" if casual <= TARGET else "✗"
        ok_h = "✓" if ha <= TARGET else "✗"
        print(f"  {name:<42} {casual:.2f}s {ok_c}   {ha:.2f}s {ok_h}")
        print(f"    └ {breakdown}")
    print()

    # Bar chart
    print("  Bar chart (各目盛り ≈ 0.1s)")
    print(f"  {'0':>4}   {'0.4':>6}   {'0.8':>6}   {'1.2':>6}   {'1.6':>6}   {'2.0+':>6}")
    print(f"  {'':>4}   {'|':>6}   {'|':>6}   {'|':>6}   {'|':>6}   {'|':>6}")
    for name, casual, ha, _ in configs:
        label = name[:30]
        b = bar(casual, scale=10)
        marker = " ← 目標 0.8s" if abs(casual - TARGET) < 0.05 else ""
        print(f"  {label:<30} {b}{marker}")
    print()

    if deepgram_stt and groq_llm:
        best = VAD_OH + stt_deepgram + groq_ttft
        print(f"  ★ Deepgram + Groq-70b 構成: 推定 {best:.2f}s (目標 {'達成' if best <= TARGET else '未達成'})")
    best_realistic = VAD_OH + stt_deepgram + llm_ttft
    print(f"  ★ Deepgram + gpt-4o  構成: 推定 {best_realistic:.2f}s (目標 {'達成' if best_realistic <= TARGET else '未達成'})")
    print()
    print("  残課題:")
    print("  - Deepgram streaming の実測 (上記は pre-recorded API = 上限値)")
    print("  - Groq の日本語精度検証 (llama-3.3-70b は英語特化)")
    print("  - VOICEVOX 合成 + AITuber Kit 発話開始まで別途 ~200-400ms 加算")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n",             type=int,  default=3)
    parser.add_argument("--wav",           type=str,  default=None)
    parser.add_argument("--skip-groq",     action="store_true")
    parser.add_argument("--skip-deepgram", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.n, args.wav, args.skip_groq, args.skip_deepgram))
