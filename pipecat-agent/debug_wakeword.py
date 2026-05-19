#!/usr/bin/env python3
"""
Wake word デバッグツール。

マイクからリアルタイムで音声を取り込み、openWakeWord のスコアを表示する。
これを使って適切な閾値と発音を確認できる。

Usage:
    .venv/bin/python debug_wakeword.py [--device 1] [--threshold 0.3]
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pyaudio
from dotenv import load_dotenv
from openwakeword.model import Model

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

import os
WAKEWORD_MODEL_PATHS = [
    str((APP_DIR / p.strip()).resolve())
    for p in os.getenv("WAKEWORD_MODEL_PATHS", "../voice-listener/models/Hey_Kemy.onnx").split(",")
    if p.strip()
]

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280  # 80ms @ 16kHz (openWakeWord 期待値)
CHUNK_BYTES = CHUNK_SAMPLES * 2  # 16-bit


def list_devices():
    p = pyaudio.PyAudio()
    print("=== 利用可能な入力デバイス ===")
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d["maxInputChannels"] > 0:
            marker = " ← デフォルト" if i == p.get_default_input_device_info()["index"] else ""
            print(f"  [{i}] {d['name']}{marker}")
    p.terminate()


def run(device_index: int | None, threshold: float):
    print(f"モデル: {WAKEWORD_MODEL_PATHS}")
    print(f"デバイス: {device_index if device_index is not None else 'デフォルト'}")
    print(f"閾値: {threshold}")
    print()
    print("モデル読み込み中...")
    model = Model(wakeword_models=WAKEWORD_MODEL_PATHS, inference_framework="onnx")
    print("マイク待受中... 話しかけてください (Ctrl+C で終了)")
    print("(スコアが閾値を超えると ★ で表示)")
    print()

    p = pyaudio.PyAudio()

    # 48kHz で取り込んで 16kHz にダウンサンプリング (デバイスが 48kHz のとき)
    device_info = p.get_device_info_by_index(device_index) if device_index is not None else p.get_default_input_device_info()
    native_rate = int(device_info["defaultSampleRate"])
    print(f"デバイス: [{device_info['index']}] {device_info['name']} (native {native_rate}Hz)")

    # PyAudio は 16kHz サポートが不安定なので native rate で取り込む
    frames_per_buffer = int(native_rate * 0.08)  # 80ms
    stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=native_rate,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=frames_per_buffer,
    )

    buffer = np.array([], dtype=np.int16)
    max_score = 0.0
    last_detected = 0.0

    try:
        while True:
            raw = stream.read(frames_per_buffer, exception_on_overflow=False)
            audio_np = np.frombuffer(raw, dtype=np.int16)

            # ダウンサンプリング (native → 16kHz)
            if native_rate != SAMPLE_RATE:
                ratio = SAMPLE_RATE / native_rate
                new_len = max(1, int(len(audio_np) * ratio))
                indices = np.round(np.linspace(0, len(audio_np) - 1, new_len)).astype(int)
                audio_np = audio_np[indices]

            buffer = np.concatenate([buffer, audio_np])

            while len(buffer) >= CHUNK_SAMPLES:
                chunk = buffer[:CHUNK_SAMPLES]
                buffer = buffer[CHUNK_SAMPLES:]

                pred = model.predict(chunk)
                if not pred:
                    continue

                for name, score in pred.items():
                    if score > 0.05:  # 微弱なスコアも表示
                        bar_len = int(score * 40)
                        bar = "█" * bar_len + "░" * (40 - bar_len)
                        ts = time.strftime("%H:%M:%S")

                        if score >= threshold:
                            print(f"\r{ts} [{bar}] {score:.3f}  ★ DETECTED: {name}        ")
                            last_detected = time.monotonic()
                            max_score = max(max_score, score)
                        elif time.monotonic() - last_detected < 2.0:
                            print(f"\r{ts} [{bar}] {score:.3f}  ({name})                   ", end="", flush=True)
                        else:
                            print(f"\r{ts} [{bar}] {score:.3f}                             ", end="", flush=True)

    except KeyboardInterrupt:
        print(f"\n\n最高スコア: {max_score:.3f}")
        print(f"推奨閾値: {max(0.1, max_score * 0.7):.2f}  (最高スコアの 70%)")
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wake word スコアデバッガ")
    parser.add_argument("--device", type=int, default=None, help="マイクデバイスインデックス")
    parser.add_argument("--threshold", type=float, default=0.3, help="表示閾値")
    parser.add_argument("--list", action="store_true", help="デバイス一覧を表示")
    args = parser.parse_args()

    if args.list:
        list_devices()
        sys.exit(0)

    run(args.device, args.threshold)
