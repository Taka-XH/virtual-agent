import os
import time
import wave
import queue
import tempfile
import shutil
import requests
import numpy as np
import sounddevice as sd
import webrtcvad
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from openwakeword.model import Model


# ============================================================
# Environment
# ============================================================

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
INTERACTION_BRIDGE_URL = os.getenv(
    "INTERACTION_BRIDGE_URL",
    "http://127.0.0.1:18089",
).rstrip("/")

STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-transcribe").strip()
STT_PROMPT = os.getenv(
    "STT_PROMPT",
    "日本語の自然な会話です。家電操作だけでなく、雑談、相談、感情表現もそのまま文字起こししてください。聞き取れない場合は無理に推測しないでください。",
).strip()

WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
OPENWAKEWORD_INFERENCE_FRAMEWORK = os.getenv(
    "OPENWAKEWORD_INFERENCE_FRAMEWORK",
    "onnx",
).strip().lower()

WAKEWORD_MODEL_PATHS = [
    str((APP_DIR / path.strip()).resolve()) if not Path(path.strip()).is_absolute() else path.strip()
    for path in os.getenv("WAKEWORD_MODEL_PATHS", "").split(",")
    if path.strip()
]

# 独自Wake Wordモデルを1つだけ使う場合は、空にして全モデル許可が安全。
# モデル名が Hey_Kemy / hey_kemy / Hey-Kemy などでズレると検出されないため。
ACCEPT_WAKE_WORDS = {
    name.strip()
    for name in os.getenv("ACCEPT_WAKE_WORDS", "").split(",")
    if name.strip()
}

WAKE_ACK_ENABLED = os.getenv("WAKE_ACK_ENABLED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

MIC_DEVICE_INDEX_RAW = os.getenv("MIC_DEVICE_INDEX", "").strip()
MIC_DEVICE_INDEX = int(MIC_DEVICE_INDEX_RAW) if MIC_DEVICE_INDEX_RAW else None
MIC_GAIN = float(os.getenv("MIC_GAIN", "1.0"))

MIN_RECORD_SECONDS = float(os.getenv("MIN_RECORD_SECONDS", "0.7"))
MIN_AUDIO_RMS = float(os.getenv("MIN_AUDIO_RMS", "0.006"))
MIN_AUDIO_PEAK = float(os.getenv("MIN_AUDIO_PEAK", "0.05"))
MIN_SPEECH_SECONDS = float(os.getenv("MIN_SPEECH_SECONDS", "0.35"))
VAD_AGGRESSIVENESS = int(os.getenv("VAD_AGGRESSIVENESS", "3"))

LAST_WAV_PATH = os.getenv("LAST_WAV_PATH", "/tmp/voice-listener-last.wav").strip()

# 自然会話では長すぎるクールダウンは不自然なので短め推奨。
COMMAND_COOLDOWN_SECONDS = float(os.getenv("COMMAND_COOLDOWN_SECONDS", "2.0"))
FALSE_WAKE_COOLDOWN_SECONDS = float(os.getenv("FALSE_WAKE_COOLDOWN_SECONDS", "1.5"))

WAKE_CONFIRM_CHUNKS = int(os.getenv("WAKE_CONFIRM_CHUNKS", "2"))
WAKE_RESET_THRESHOLD = float(os.getenv("WAKE_RESET_THRESHOLD", str(WAKE_THRESHOLD * 0.75)))

# AITuberKitやOpenClawの返答音声をWake Wordとして拾わないための短い抑制時間。
WAKE_DEBOUNCE_SECONDS = float(os.getenv("WAKE_DEBOUNCE_SECONDS", "2.0"))

# 同じ発話が二重送信されることだけ防ぐ。自然会話用なので短め。
REPEAT_TEXT_IGNORE_SECONDS = float(os.getenv("REPEAT_TEXT_IGNORE_SECONDS", "3.0"))

SAMPLE_RATE = 16000
CHANNELS = 1

# openWakeWordは80ms程度のchunkで扱いやすい
WAKE_CHUNK_SAMPLES = 1280  # 16kHz * 0.08 sec

# VADは20ms frameで使う
VAD_FRAME_MS = 20
VAD_FRAME_SAMPLES = int(SAMPLE_RATE * VAD_FRAME_MS / 1000)

# 発話終了判定
MAX_RECORD_SECONDS = float(os.getenv("MAX_RECORD_SECONDS", "15"))
START_PADDING_SECONDS = float(os.getenv("START_PADDING_SECONDS", "0.4"))
END_SILENCE_SECONDS = float(os.getenv("END_SILENCE_SECONDS", "0.9"))
VAD_START_FRAMES = int(os.getenv("VAD_START_FRAMES", "4"))

# STTが無音や動画音声で出しがちな定型誤認識だけ除外。
IGNORED_TRANSCRIPTS = {
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "最後までご視聴いただきありがとうございます",
    "最後までご視聴いただきありがとうございます。",
    "ありがとうございました",
    "ありがとうございました。",
}

audio_q: queue.Queue[np.ndarray] = queue.Queue()
audio_remainder = np.array([], dtype=np.float32)


# ============================================================
# Audio helpers
# ============================================================

def audio_callback(indata, frames, time_info, status):
    if status:
        print(f"[audio] {status}")

    mono = np.clip(indata[:, 0] * MIC_GAIN, -1.0, 1.0).copy()
    audio_q.put(mono)


def pcm_float_to_int16(audio: np.ndarray) -> np.ndarray:
    audio = np.clip(audio, -1.0, 1.0)
    return (audio * 32767).astype(np.int16)


def write_wav(path: str, pcm16: np.ndarray):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())


def get_audio_stats(pcm16: np.ndarray) -> tuple[float, float, float]:
    if len(pcm16) == 0:
        return 0.0, 0.0, 0.0

    audio = pcm16.astype(np.float32) / 32768.0
    duration = len(pcm16) / SAMPLE_RATE
    rms = float(np.sqrt(np.mean(np.square(audio))))
    peak = float(np.max(np.abs(audio)))

    return duration, rms, peak


def get_vad_speech_seconds(
    pcm16: np.ndarray,
    aggressiveness: int = VAD_AGGRESSIVENESS,
) -> float:
    if len(pcm16) < VAD_FRAME_SAMPLES:
        return 0.0

    vad = webrtcvad.Vad(aggressiveness)
    speech_frames = 0
    frame_count = len(pcm16) // VAD_FRAME_SAMPLES

    for i in range(frame_count):
        start = i * VAD_FRAME_SAMPLES
        frame = pcm16[start:start + VAD_FRAME_SAMPLES]

        if vad.is_speech(frame.tobytes(), SAMPLE_RATE):
            speech_frames += 1

    return speech_frames * VAD_FRAME_MS / 1000


def drain_queue():
    global audio_remainder

    audio_remainder = np.array([], dtype=np.float32)

    while not audio_q.empty():
        try:
            audio_q.get_nowait()
        except queue.Empty:
            break


def get_audio_samples(num_samples: int) -> np.ndarray:
    """キューから必要サンプル数を集める。"""
    global audio_remainder

    chunks = []
    total = len(audio_remainder)

    if total:
        chunks.append(audio_remainder)
        audio_remainder = np.array([], dtype=np.float32)

    while total < num_samples:
        chunk = audio_q.get()
        chunks.append(chunk)
        total += len(chunk)

    audio = np.concatenate(chunks)

    if len(audio) > num_samples:
        audio_remainder = audio[num_samples:]

    return audio[:num_samples]


# ============================================================
# External calls
# ============================================================

def speak_status(text: str, emotion: str = "neutral"):
    """
    AITuberKitに短いステータスを喋らせる。
    WAKE_ACK_ENABLED=falseなら通常は使わない。
    """
    try:
        requests.post(
            f"{INTERACTION_BRIDGE_URL}/speak",
            json={"text": text, "emotion": emotion},
            timeout=3,
        )
    except Exception as e:
        print(f"[warn] speak_status failed: {e}")


def send_text_to_openclaw(text: str):
    """
    STT結果をそのままinteraction-bridge経由でOpenClawへ送る。
    家電操作か雑談かの判断はOpenClaw側に任せる。
    """
    res = requests.post(
        f"{INTERACTION_BRIDGE_URL}/send-text",
        json={"text": text},
        timeout=60,
    )
    res.raise_for_status()
    return res.json()


def transcribe_wav(path: str) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)

    with open(path, "rb") as f:
        result = client.audio.transcriptions.create(
            model=STT_MODEL,
            file=f,
            language="ja",
            prompt=STT_PROMPT,
            temperature=0,
        )

    return result.text.strip()


# ============================================================
# Wake word helpers
# ============================================================

def get_accepted_prediction(prediction: dict[str, float]) -> tuple[str, float] | None:
    """
    ACCEPT_WAKE_WORDS が空なら全モデルを許可。
    指定がある場合だけ名前で絞る。
    """
    accepted_prediction = {
        name: score
        for name, score in prediction.items()
        if not ACCEPT_WAKE_WORDS or name in ACCEPT_WAKE_WORDS
    }

    if not accepted_prediction:
        return None

    return max(accepted_prediction.items(), key=lambda x: x[1])


def reset_wake_model(wake_model: Model):
    wake_model.reset()


def reset_wake_state(wake_model: Model):
    reset_wake_model(wake_model)
    drain_queue()


def reject_utterance(wav_path: str, wake_model: Model):
    try:
        os.remove(wav_path)
    except OSError:
        pass

    reset_wake_state(wake_model)
    time.sleep(FALSE_WAKE_COOLDOWN_SECONDS)
    reset_wake_state(wake_model)


# ============================================================
# Conversation filters
# ============================================================

def normalize_spoken_text(text: str) -> str:
    """
    自然会話用の最低限の整形。
    家電操作向けの正規化はしない。
    """
    normalized = text.strip()
    normalized = normalized.lstrip("、。,. !?！？ \n\t")
    return normalized


def should_ignore_transcript(text: str) -> bool:
    """
    自然会話用なので、基本的には捨てない。
    明らかな空文字・定型誤認識だけ除外する。
    """
    normalized = normalize_spoken_text(text)

    if not normalized:
        return True

    if normalized in IGNORED_TRANSCRIPTS:
        return True

    # 1文字だけの「あ」「え」などは誤検知の可能性が高い。
    if len(normalized) <= 1:
        return True

    return False


def is_recent_repeat(
    current_text: str,
    last_text: str,
    last_time: float | None,
    repeat_seconds: float = REPEAT_TEXT_IGNORE_SECONDS,
) -> bool:
    if not last_time:
        return False

    if current_text != last_text:
        return False

    return time.monotonic() - last_time < repeat_seconds


# ============================================================
# Recording
# ============================================================

def record_utterance() -> np.ndarray:
    """
    Wake Word検出後、VADで発話区間だけ録音する。
    発話開始前の少しの音もpaddingとして残す。
    """
    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)

    max_frames = int(MAX_RECORD_SECONDS * 1000 / VAD_FRAME_MS)
    end_silence_frames = int(END_SILENCE_SECONDS * 1000 / VAD_FRAME_MS)
    padding_frames = int(START_PADDING_SECONDS * 1000 / VAD_FRAME_MS)

    ring_buffer: list[np.ndarray] = []
    recorded: list[np.ndarray] = []

    triggered = False
    silence_count = 0
    speech_streak = 0

    print("[state] listening for utterance...")

    for _ in range(max_frames):
        frame_float = get_audio_samples(VAD_FRAME_SAMPLES)
        frame_int16 = pcm_float_to_int16(frame_float)
        is_speech = vad.is_speech(frame_int16.tobytes(), SAMPLE_RATE)

        if not triggered:
            ring_buffer.append(frame_int16)

            if len(ring_buffer) > padding_frames:
                ring_buffer.pop(0)

            if is_speech:
                speech_streak += 1
            else:
                speech_streak = 0

            if speech_streak >= VAD_START_FRAMES:
                triggered = True
                recorded.extend(ring_buffer)
                ring_buffer.clear()
                recorded.append(frame_int16)
                print("[state] speech started")

        else:
            recorded.append(frame_int16)

            if is_speech:
                silence_count = 0
            else:
                silence_count += 1

            if silence_count >= end_silence_frames:
                print("[state] speech ended")
                break

    if not recorded:
        return np.array([], dtype=np.int16)

    return np.concatenate(recorded)


# ============================================================
# Main
# ============================================================

def main():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")

    print(f"[init] loading openWakeWord model ({OPENWAKEWORD_INFERENCE_FRAMEWORK})...")

    wake_model = Model(
        wakeword_models=WAKEWORD_MODEL_PATHS,
        inference_framework=OPENWAKEWORD_INFERENCE_FRAMEWORK,
    )

    if WAKEWORD_MODEL_PATHS:
        print(f"[init] wake word model paths: {', '.join(WAKEWORD_MODEL_PATHS)}")

    print(f"[init] accepted wake words: {', '.join(sorted(ACCEPT_WAKE_WORDS)) or 'all'}")
    print(f"[init] stt model: {STT_MODEL}")
    print(f"[init] vad aggressiveness: {VAD_AGGRESSIVENESS}")

    print("[init] starting microphone stream...")
    print(f"[init] device index: {MIC_DEVICE_INDEX}")
    print(f"[init] mic gain: {MIC_GAIN}")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=WAKE_CHUNK_SAMPLES,
        callback=audio_callback,
        device=MIC_DEVICE_INDEX,
    ):
        print("[ready] Wake Word待受中です。Ctrl+Cで終了します。")

        wake_candidate_name = ""
        wake_candidate_score = 0.0
        wake_confirm_count = 0

        last_text = ""
        last_text_time: float | None = None

        while True:
            audio_float = get_audio_samples(WAKE_CHUNK_SAMPLES)
            pcm16 = pcm_float_to_int16(audio_float)

            prediction = wake_model.predict(pcm16)
            accepted_prediction = get_accepted_prediction(prediction)

            if accepted_prediction is None:
                wake_confirm_count = 0
                continue

            best_name, best_score = accepted_prediction

            if best_score >= WAKE_THRESHOLD:
                if best_name == wake_candidate_name:
                    wake_confirm_count += 1
                    wake_candidate_score = max(wake_candidate_score, best_score)
                else:
                    wake_candidate_name = best_name
                    wake_candidate_score = best_score
                    wake_confirm_count = 1

            elif best_score < WAKE_RESET_THRESHOLD:
                wake_candidate_name = ""
                wake_candidate_score = 0.0
                wake_confirm_count = 0

            if wake_confirm_count < WAKE_CONFIRM_CHUNKS:
                continue

            print(
                f"[wake] detected: {wake_candidate_name} "
                f"score={wake_candidate_score:.3f} confirms={wake_confirm_count}"
            )

            wake_candidate_name = ""
            wake_candidate_score = 0.0
            wake_confirm_count = 0

            # Wakeモデルの内部状態だけリセット。
            # 音声キューを捨てると、Wake Word直後の発話冒頭が欠ける場合がある。
            reset_wake_model(wake_model)

            if WAKE_ACK_ENABLED:
                speak_status("はい、聞いています。", "neutral")
                time.sleep(1.0)
                reset_wake_state(wake_model)

            utterance = record_utterance()
            duration, rms, peak = get_audio_stats(utterance)
            speech_seconds = get_vad_speech_seconds(utterance)

            print(
                f"[audio] duration={duration:.2f}s speech={speech_seconds:.2f}s "
                f"rms={rms:.4f} peak={peak:.4f}"
            )

            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                wav_path = tmp.name

            write_wav(wav_path, utterance)

            if LAST_WAV_PATH:
                shutil.copyfile(wav_path, LAST_WAV_PATH)
                print(f"[debug] saved last wav: {LAST_WAV_PATH}")

            if duration < MIN_RECORD_SECONDS:
                print("[warn] utterance too short")
                reject_utterance(wav_path, wake_model)
                continue

            if rms < MIN_AUDIO_RMS:
                print("[warn] utterance too quiet; skip transcription")
                reject_utterance(wav_path, wake_model)
                continue

            if peak < MIN_AUDIO_PEAK:
                print("[warn] utterance peak too low; skip transcription")
                reject_utterance(wav_path, wake_model)
                continue

            if speech_seconds < MIN_SPEECH_SECONDS:
                print("[warn] utterance has too little speech; skip transcription")
                reject_utterance(wav_path, wake_model)
                continue

            try:
                print("[state] transcribing...")
                text = transcribe_wav(wav_path)
                text = normalize_spoken_text(text)

                print(f"[stt] {text}")

                if should_ignore_transcript(text):
                    print("[warn] ignored empty/noise transcription")
                    continue

                if is_recent_repeat(text, last_text, last_text_time):
                    print("[warn] ignored recent repeated text")
                    continue

                print("[state] sending to OpenClaw...")
                result = send_text_to_openclaw(text)
                print(f"[openclaw] {result}")

                last_text = text
                last_text_time = time.monotonic()

                reset_wake_state(wake_model)

            except Exception as e:
                print(f"[error] {e}")
                speak_status("処理中にエラーが発生しました。", "sad")

            finally:
                try:
                    os.remove(wav_path)
                except OSError:
                    pass

            # キャラの返答音声を再度拾わないためのクールダウン
            print("[state] cooldown...")
            time.sleep(COMMAND_COOLDOWN_SECONDS)
            reset_wake_state(wake_model)

            cooldown_until = time.monotonic() + WAKE_DEBOUNCE_SECONDS

            while time.monotonic() < cooldown_until:
                audio_float = get_audio_samples(WAKE_CHUNK_SAMPLES)
                pcm16 = pcm_float_to_int16(audio_float)
                wake_model.predict(pcm16)

            reset_wake_state(wake_model)
            print("[ready] Wake Word待受に戻りました。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[exit] stopped")