#!/usr/bin/env python3
"""
STT accuracy & LLM response accuracy benchmark for pipecat-agent.

音声生成: macOS say コマンド (Kyoko/Reed) → 16kHz WAV
STT比較 : OpenAI whisper-1 / gpt-4o-mini-transcribe / gpt-4o-transcribe
          + Deepgram nova-2 / nova-3  (DEEPGRAM_API_KEY が必要)
LLM比較 : OpenAI gpt-4o / gpt-4o-mini
          + Groq llama-3.1-8b / llama-3.3-70b  (GROQ_API_KEY が必要)

STT指標: CER (文字誤り率)、完全一致率、HA キーワード一致率
LLM指標: HA ツールコール正解率、正しいアクション名、日本語応答率

Usage:
    .venv/bin/python bench_accuracy.py [--voices Kyoko Reed] [--reps 2]
"""

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
if not os.getenv("OPENAI_API_KEY"):
    load_dotenv(APP_DIR / "../voice-listener/.env")

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")

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

SYSTEM_PROMPT = (
    "あなたは家のAIキャラクターです。日本語で1〜2文で答えてください。"
    "家電操作を頼まれたら必ずツールを使ってください。"
)

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# (reference_text, category, ha_keywords)
STT_CASES = [
    # HA commands (最重要)
    ("洗面所の電気をつけてください",      "HA_clear",   ["洗面所", "電気", "つけ"]),
    ("洗面所の電気を消して",              "HA_clear",   ["洗面所", "電気", "消"]),
    ("電気をつけてください",              "HA_short",   ["電気", "つけ"]),
    ("電気を消してください",              "HA_short",   ["電気", "消"]),
    ("洗面所のライトをつけてください",    "HA_polite",  ["洗面所", "ライト", "つけ"]),
    # Casual conversation
    ("こんにちは今日もよろしくお願いします", "casual",  ["こんにちは", "よろしく"]),
    ("最近のおすすめ映画を教えてください",  "casual",  ["映画", "おすすめ"]),
    ("今日は少し疲れました",               "casual",  ["疲れ"]),
    # Short/tricky
    ("ありがとうございます",              "short",      ["ありがとう"]),
    ("はい分かりました",                  "short",      ["分かり"]),
]

# (prompt, should_call_tool, expected_action, category)
LLM_CASES = [
    # HA — 明示的
    ("洗面所の電気をつけて",              True,  "bathroom_light_on",  "HA_explicit"),
    ("洗面所の電気を消して",              True,  "bathroom_light_off", "HA_explicit"),
    # HA — 省略形
    ("電気つけて",                        True,  "bathroom_light_on",  "HA_short"),
    ("電気消して",                        True,  "bathroom_light_off", "HA_short"),
    # HA — 敬語
    ("洗面所のライトをつけていただけますか", True, "bathroom_light_on", "HA_polite"),
    ("洗面所の照明を消していただけますか", True,  "bathroom_light_off", "HA_polite"),
    # Casual — ツールコール不要
    ("こんにちは！",                      False, None,                 "casual"),
    ("今日の天気はどうですか",            False, None,                 "casual"),
    ("最近疲れています",                  False, None,                 "casual"),
    ("おすすめの映画を教えて",            False, None,                 "casual"),
]

# ---------------------------------------------------------------------------
# Audio generation (macOS say → 16kHz WAV)
# ---------------------------------------------------------------------------

def generate_wav_macos(text: str, voice: str = "Kyoko") -> bytes:
    with tempfile.TemporaryDirectory() as d:
        aiff = os.path.join(d, "out.aiff")
        wav  = os.path.join(d, "out.wav")
        subprocess.run(
            ["say", "-v", voice, "-o", aiff, text],
            check=True, capture_output=True
        )
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", aiff, wav],
            check=True, capture_output=True
        )
        return Path(wav).read_bytes()

# ---------------------------------------------------------------------------
# CER (Character Error Rate) helpers
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """句読点・スペースを除いた文字列に正規化。"""
    remove = " \t　。、．，！？!?「」『』【】（）()"
    return "".join(c for c in s if c not in remove)

def levenshtein(a: str, b: str) -> int:
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            prev, dp[j] = dp[j], prev if a[i-1] == b[j-1] else 1 + min(prev, dp[j], dp[j-1])
    return dp[n]

def cer(hypothesis: str, reference: str) -> float:
    ref = _normalize(reference)
    hyp = _normalize(hypothesis)
    if not ref:
        return 0.0
    return min(levenshtein(hyp, ref) / len(ref), 1.0)

def keyword_match_rate(hypothesis: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    matched = sum(1 for kw in keywords if kw in hypothesis)
    return matched / len(keywords)

def is_japanese(text: str) -> bool:
    jp = sum(1 for c in text if "぀" <= c <= "鿿" or "一" <= c <= "鿿")
    return jp >= max(1, len(text) * 0.25)

# ---------------------------------------------------------------------------
# STT: OpenAI batch
# ---------------------------------------------------------------------------

def stt_openai(wav_bytes: bytes, model: str, client: OpenAI) -> str:
    buf = io.BytesIO(wav_bytes)
    buf.name = "speech.wav"
    result = client.audio.transcriptions.create(
        model=model, file=buf, language="ja", temperature=0
    )
    return result.text.strip()

# ---------------------------------------------------------------------------
# STT: Deepgram pre-recorded
# ---------------------------------------------------------------------------

async def stt_deepgram(wav_bytes: bytes, model: str) -> str:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": model, "language": "ja", "punctuate": "true", "smart_format": "true"},
            headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/wav"},
            content=wav_bytes,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    return (data.get("results", {})
               .get("channels", [{}])[0]
               .get("alternatives", [{}])[0]
               .get("transcript", "")).strip()

# ---------------------------------------------------------------------------
# LLM: OpenAI (non-streaming for clean tool_call inspection)
# ---------------------------------------------------------------------------

def llm_openai(prompt: str, model: str, client: OpenAI) -> dict:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        tools=HA_TOOLS,
        tool_choice="auto",
        max_tokens=200,
    )
    msg = resp.choices[0].message
    tool_calls = msg.tool_calls or []
    tool_called = bool(tool_calls)
    action = None
    if tool_called:
        try:
            args = json.loads(tool_calls[0].function.arguments)
            action = args.get("action")
        except Exception:
            pass
    text = msg.content or ""
    return {"tool_called": tool_called, "action": action, "text": text}

# ---------------------------------------------------------------------------
# LLM: Groq (OpenAI-compatible)
# ---------------------------------------------------------------------------

async def llm_groq(prompt: str, model: str) -> dict:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "tools": HA_TOOLS,
        "tool_choice": "auto",
        "max_tokens": 200,
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    msg = data["choices"][0]["message"]
    tool_calls = msg.get("tool_calls") or []
    tool_called = bool(tool_calls)
    action = None
    if tool_called:
        try:
            args = json.loads(tool_calls[0]["function"]["arguments"])
            action = args.get("action")
        except Exception:
            pass
    text = msg.get("content") or ""
    return {"tool_called": tool_called, "action": action, "text": text}

# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class STTResult:
    backend: str
    cer_values: list[float] = field(default_factory=list)
    exact_matches: list[bool] = field(default_factory=list)
    kw_rates: list[float] = field(default_factory=list)
    errors: int = 0

    @property
    def mean_cer(self):
        return sum(self.cer_values) / len(self.cer_values) if self.cer_values else float("nan")

    @property
    def exact_rate(self):
        return sum(self.exact_matches) / len(self.exact_matches) * 100 if self.exact_matches else 0.0

    @property
    def kw_rate(self):
        return sum(self.kw_rates) / len(self.kw_rates) * 100 if self.kw_rates else 0.0


@dataclass
class LLMResult:
    backend: str
    cases: list[dict] = field(default_factory=list)  # {expected_tool, expected_action, result}

    @property
    def ha_cases(self):
        return [c for c in self.cases if c["expected_tool"]]

    @property
    def casual_cases(self):
        return [c for c in self.cases if not c["expected_tool"]]

    @property
    def tool_call_accuracy(self):
        if not self.ha_cases:
            return 0.0
        correct = sum(1 for c in self.ha_cases if c["result"]["tool_called"] == c["expected_tool"])
        return correct / len(self.ha_cases) * 100

    @property
    def action_accuracy(self):
        if not self.ha_cases:
            return 0.0
        correct = sum(
            1 for c in self.ha_cases
            if c["result"]["tool_called"] and c["result"]["action"] == c["expected_action"]
        )
        return correct / len(self.ha_cases) * 100

    @property
    def no_false_tool_rate(self):
        if not self.casual_cases:
            return 0.0
        correct = sum(1 for c in self.casual_cases if not c["result"]["tool_called"])
        return correct / len(self.casual_cases) * 100

    @property
    def japanese_rate(self):
        texts = [c["result"]["text"] for c in self.cases if c["result"]["text"]]
        if not texts:
            return 0.0
        return sum(1 for t in texts if is_japanese(t)) / len(texts) * 100


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

SEP = "=" * 72

def section(title: str):
    print(f"\n{SEP}\n  {title}\n{SEP}")

def star(v: float, thresholds=(90, 75, 60, 40)) -> str:
    for i, t in enumerate(thresholds):
        if v >= t:
            return "★" * (5 - i) + "☆" * i
    return "☆☆☆☆☆"

def pct_bar(v: float, width: int = 20) -> str:
    filled = int(v / 100 * width)
    return "█" * filled + "░" * (width - filled)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(voices: list[str], reps: int, skip_deepgram: bool, skip_groq: bool):
    if not OPENAI_API_KEY:
        sys.exit("ERROR: OPENAI_API_KEY が未設定です")

    client = OpenAI(api_key=OPENAI_API_KEY)
    use_deepgram = bool(DEEPGRAM_API_KEY) and not skip_deepgram
    use_groq     = bool(GROQ_API_KEY)     and not skip_groq

    print(SEP)
    print("  STT & LLM Accuracy Benchmark")
    print(f"  音声: macOS say ({', '.join(voices)}) × {reps} reps  |  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  STT: OpenAI ✓  Deepgram {'✓' if use_deepgram else '✗ (key未設定)'}")
    print(f"  LLM: OpenAI ✓  Groq     {'✓' if use_groq else '✗ (key未設定)'}")
    print(SEP)

    total_items = len(STT_CASES) * len(voices) * reps
    print(f"\n[生成] {total_items} 件の音声を生成します...")

    # ---------------------------------------------------------------------------
    # Generate all audio upfront
    # ---------------------------------------------------------------------------
    audio_items: list[tuple[str, list[str], bytes]] = []  # (ref, keywords, wav)
    for ref, _cat, keywords in STT_CASES:
        wavs = []
        for voice in voices:
            for _ in range(reps):
                wav = generate_wav_macos(ref, voice)
                wavs.append(wav)
        # combine all variations as separate items
        for voice in voices:
            for r in range(reps):
                idx = voices.index(voice) * reps + r
                audio_items.append((ref, keywords, wavs[idx]))
        print(f"  ✓ 「{ref[:20]}」  ({len(voices)} voices × {reps} reps)")

    print(f"\n計 {len(audio_items)} 音声ファイル生成完了")

    # ---------------------------------------------------------------------------
    # [A] STT Accuracy
    # ---------------------------------------------------------------------------
    section("[A] STT 精度テスト")

    stt_backends: dict[str, STTResult] = {}

    openai_stt_models = ["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"]
    deepgram_models   = ["nova-2-general", "nova-3-general"] if use_deepgram else []

    for model in openai_stt_models:
        key = f"OpenAI/{model}"
        stt_backends[key] = STTResult(backend=key)

    for model in deepgram_models:
        key = f"Deepgram/{model}"
        stt_backends[key] = STTResult(backend=key)

    for i, (ref, keywords, wav) in enumerate(audio_items):
        print(f"\r  STT [{i+1}/{len(audio_items)}] 「{ref[:18]}」...", end="", flush=True)

        # OpenAI
        for model in openai_stt_models:
            key = f"OpenAI/{model}"
            try:
                hyp = stt_openai(wav, model, client)
                c = cer(hyp, ref)
                em = _normalize(hyp) == _normalize(ref)
                kw = keyword_match_rate(hyp, keywords)
                stt_backends[key].cer_values.append(c)
                stt_backends[key].exact_matches.append(em)
                stt_backends[key].kw_rates.append(kw)
            except Exception as e:
                stt_backends[key].errors += 1
            time.sleep(0.2)

        # Deepgram
        for model in deepgram_models:
            key = f"Deepgram/{model}"
            try:
                hyp = await stt_deepgram(wav, model)
                c = cer(hyp, ref)
                em = _normalize(hyp) == _normalize(ref)
                kw = keyword_match_rate(hyp, keywords)
                stt_backends[key].cer_values.append(c)
                stt_backends[key].exact_matches.append(em)
                stt_backends[key].kw_rates.append(kw)
            except Exception as e:
                stt_backends[key].errors += 1
            await asyncio.sleep(0.2)

    print("\n")

    # --- STT detail table ---
    print(f"  {'バックエンド':<36} {'CER↓':>7} {'完全一致↑':>9} {'KW一致↑':>9} {'評価':>7} {'エラー':>6}")
    print("  " + "-" * 70)
    for key, r in stt_backends.items():
        score_for_star = (1 - r.mean_cer) * 40 + r.exact_rate * 0.3 + r.kw_rate * 0.3
        print(
            f"  {key:<36} "
            f"{r.mean_cer*100:>6.1f}%  "
            f"{r.exact_rate:>8.1f}%  "
            f"{r.kw_rate:>8.1f}%  "
            f"{star(score_for_star):>7}  "
            f"{r.errors:>5}"
        )

    # --- Per-sentence STT detail ---
    print("\n  ── 文ごとの詳細 (gpt-4o-transcribe vs Deepgram/nova-3) ──")
    cmp_a = "OpenAI/gpt-4o-transcribe"
    cmp_b = "Deepgram/nova-3-general" if use_deepgram else None
    print(f"\n  {'テスト文':<26} {'カテゴリ':<10} {cmp_a.split('/')[-1]:>22}", end="")
    if cmp_b:
        print(f"  {cmp_b.split('/')[-1]:>16}", end="")
    print()
    print("  " + "-" * (60 + (20 if cmp_b else 0)))

    # rebuild per-sentence stats from audio_items
    per_sent_a: dict[str, list[float]] = {}
    per_sent_b: dict[str, list[float]] = {}
    for i, (ref, keywords, wav) in enumerate(audio_items):
        try:
            hyp_a = stt_openai(wav, "gpt-4o-transcribe", client)
            per_sent_a.setdefault(ref, []).append(cer(hyp_a, ref))
        except Exception:
            per_sent_a.setdefault(ref, []).append(1.0)
        time.sleep(0.15)

        if cmp_b:
            try:
                hyp_b = await stt_deepgram(wav, "nova-3-general")
                per_sent_b.setdefault(ref, []).append(cer(hyp_b, ref))
            except Exception:
                per_sent_b.setdefault(ref, []).append(1.0)
            await asyncio.sleep(0.15)

    for (ref, cat, _) in STT_CASES:
        vals_a = per_sent_a.get(ref, [])
        mean_a = sum(vals_a)/len(vals_a) if vals_a else float("nan")
        row = f"  {ref[:25]:<26} {cat:<10} CER={mean_a*100:>5.1f}%"
        if cmp_b:
            vals_b = per_sent_b.get(ref, [])
            mean_b = sum(vals_b)/len(vals_b) if vals_b else float("nan")
            row += f"  CER={mean_b*100:>5.1f}%"
            winner = "← Deepgram" if mean_b < mean_a - 0.02 else ("← OpenAI" if mean_a < mean_b - 0.02 else "  同等")
            row += f"  {winner}"
        print(row)

    # ---------------------------------------------------------------------------
    # [B] LLM Response Accuracy
    # ---------------------------------------------------------------------------
    section("[B] LLM 応答精度テスト  (HA ツールコール正解率 + 日本語率)")

    llm_backends: dict[str, LLMResult] = {}
    llm_openai_models = ["gpt-4o-mini", "gpt-4o"]
    llm_groq_models   = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"] if use_groq else []

    for model in llm_openai_models:
        llm_backends[f"OpenAI/{model}"] = LLMResult(backend=f"OpenAI/{model}")
    for model in llm_groq_models:
        llm_backends[f"Groq/{model}"] = LLMResult(backend=f"Groq/{model}")

    total_llm = len(LLM_CASES) * (len(llm_openai_models) + len(llm_groq_models)) * reps
    done = 0
    for i in range(reps):
        for prompt, exp_tool, exp_action, cat in LLM_CASES:
            print(f"\r  LLM [{done+1}/{total_llm}] 「{prompt[:16]}」...", end="", flush=True)

            # OpenAI
            for model in llm_openai_models:
                key = f"OpenAI/{model}"
                try:
                    result = llm_openai(prompt, model, client)
                    llm_backends[key].cases.append({
                        "prompt": prompt, "category": cat,
                        "expected_tool": exp_tool, "expected_action": exp_action,
                        "result": result,
                    })
                except Exception as e:
                    llm_backends[key].cases.append({
                        "prompt": prompt, "category": cat,
                        "expected_tool": exp_tool, "expected_action": exp_action,
                        "result": {"tool_called": False, "action": None, "text": ""},
                    })
                done += 1
                time.sleep(0.4)

            # Groq
            for model in llm_groq_models:
                key = f"Groq/{model}"
                try:
                    result = await llm_groq(prompt, model)
                    llm_backends[key].cases.append({
                        "prompt": prompt, "category": cat,
                        "expected_tool": exp_tool, "expected_action": exp_action,
                        "result": result,
                    })
                except Exception as e:
                    llm_backends[key].cases.append({
                        "prompt": prompt, "category": cat,
                        "expected_tool": exp_tool, "expected_action": exp_action,
                        "result": {"tool_called": False, "action": None, "text": ""},
                    })
                done += 1
                await asyncio.sleep(0.4)

    print("\n")

    # --- LLM summary table ---
    print(f"  {'バックエンド':<32} {'HA呼出↑':>9} {'正Action↑':>9} {'誤呼出↓':>9} {'日本語率↑':>9} {'総合':>7}")
    print("  " + "-" * 72)
    for key, r in llm_backends.items():
        # false positive: casual でツールを呼んでしまう率
        false_pos = 100 - r.no_false_tool_rate
        composite = r.tool_call_accuracy * 0.35 + r.action_accuracy * 0.35 + r.no_false_tool_rate * 0.15 + r.japanese_rate * 0.15
        print(
            f"  {key:<32} "
            f"{r.tool_call_accuracy:>8.1f}%  "
            f"{r.action_accuracy:>8.1f}%  "
            f"{false_pos:>8.1f}%  "
            f"{r.japanese_rate:>8.1f}%  "
            f"{star(composite):>7}"
        )

    # --- LLM per-prompt detail ---
    print("\n  ── プロンプトごとの詳細 (gpt-4o vs Groq/llama-70b) ──")
    cmp_llm_a = "OpenAI/gpt-4o"
    cmp_llm_b = "Groq/llama-3.3-70b-versatile" if use_groq else None

    print(f"\n  {'プロンプト':<26} {'期待':>10}  {cmp_llm_a.split('/')[-1]:>18}", end="")
    if cmp_llm_b:
        print(f"  {cmp_llm_b.split('/')[-1]:>24}", end="")
    print()
    print("  " + "-" * (62 + (28 if cmp_llm_b else 0)))

    def fmt_result(case: dict | None) -> str:
        if case is None:
            return "N/A"
        r = case["result"]
        if case["expected_tool"]:
            if r["tool_called"] and r["action"] == case["expected_action"]:
                return "✓ " + (r["action"] or "")
            elif r["tool_called"]:
                return "✗ " + (r["action"] or "wrong")
            else:
                return "✗ no_call"
        else:
            jp = "JP✓" if is_japanese(r["text"]) else "JP✗"
            called = "TOOL!" if r["tool_called"] else ""
            return f"{jp} {called}".strip()

    # gather one representative case per prompt per backend
    for prompt, exp_tool, exp_action, cat in LLM_CASES:
        exp_str = f"call({exp_action.split('_')[1:3]})" if exp_tool and exp_action else "casual"
        cases_a = [c for c in llm_backends.get(cmp_llm_a, LLMResult("")).cases if c["prompt"] == prompt]
        row = f"  {prompt[:25]:<26} {exp_str:>10}  {fmt_result(cases_a[0] if cases_a else None):>18}"
        if cmp_llm_b:
            cases_b = [c for c in llm_backends.get(cmp_llm_b, LLMResult("")).cases if c["prompt"] == prompt]
            row += f"  {fmt_result(cases_b[0] if cases_b else None):>24}"
        print(row)

    # ---------------------------------------------------------------------------
    # SUMMARY
    # ---------------------------------------------------------------------------
    section("SUMMARY — 総合評価")

    print("""
  ┌─────────────────────────────────────────────────────────────────────┐
  │ STT 推奨                                                             │
  ├─────────────────────────────────────────────────────────────────────┤""")

    stt_scores = {}
    for key, r in stt_backends.items():
        stt_scores[key] = (1 - r.mean_cer) * 50 + r.kw_rate * 50
    best_stt = max(stt_scores, key=stt_scores.get)
    for key, score in sorted(stt_scores.items(), key=lambda x: -x[1]):
        r = stt_backends[key]
        marker = " ← 推奨" if key == best_stt else ""
        print(f"  │ {key:<30} CER={r.mean_cer*100:.1f}%  KW={r.kw_rate:.0f}%  {pct_bar(score)}{marker}")

    print("""  └─────────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────────┐
  │ LLM 推奨                                                             │
  ├─────────────────────────────────────────────────────────────────────┤""")

    llm_scores = {}
    for key, r in llm_backends.items():
        llm_scores[key] = r.tool_call_accuracy * 0.4 + r.action_accuracy * 0.4 + r.no_false_tool_rate * 0.1 + r.japanese_rate * 0.1
    best_llm = max(llm_scores, key=llm_scores.get) if llm_scores else None
    for key, score in sorted(llm_scores.items(), key=lambda x: -x[1]):
        r = llm_backends[key]
        marker = " ← 推奨" if key == best_llm else ""
        print(f"  │ {key:<30} HA={r.tool_call_accuracy:.0f}%  JP={r.japanese_rate:.0f}%  {pct_bar(score)}{marker}")

    print("""  └─────────────────────────────────────────────────────────────────────┘""")

    print(f"""
  凡例:
    CER   = 文字誤り率 (低いほど良い)
    KW    = HA キーワード一致率 (高いほど良い)
    HA    = HA ツールコール正解率
    JP    = 日本語で応答した割合
    誤呼出 = casual 発話でツールを誤って呼んだ割合 (低いほど良い)
""")

    if not use_deepgram:
        print("  ⚠ Deepgram: DEEPGRAM_API_KEY を .env に設定すると比較可能です")
        print("    → https://console.deepgram.com/")
    if not use_groq:
        print("  ⚠ Groq: GROQ_API_KEY を .env に設定すると比較可能です")
        print("    → https://console.groq.com/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--voices", nargs="+", default=["Kyoko", "Reed"],
                        help="macOS TTS voices (default: Kyoko Reed)")
    parser.add_argument("--reps",   type=int, default=1,
                        help="repetitions per voice per sentence (default: 1)")
    parser.add_argument("--skip-deepgram", action="store_true")
    parser.add_argument("--skip-groq",     action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.voices, args.reps, args.skip_deepgram, args.skip_groq))
