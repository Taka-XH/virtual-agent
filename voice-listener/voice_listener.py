import os
import time
import wave
import queue
import tempfile
import shutil
import re
import requests
import numpy as np
import sounddevice as sd
import webrtcvad
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from openwakeword.model import Model

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
INTERACTION_BRIDGE_URL = os.getenv("INTERACTION_BRIDGE_URL", "http://127.0.0.1:18089").rstrip("/")
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-transcribe").strip()
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
OPENWAKEWORD_INFERENCE_FRAMEWORK = os.getenv("OPENWAKEWORD_INFERENCE_FRAMEWORK", "onnx").strip().lower()
ACCEPT_WAKE_WORDS = {
    name.strip()
    for name in os.getenv("ACCEPT_WAKE_WORDS", "hey_jarvis").split(",")
    if name.strip()
}
SMART_HOME_KEYWORDS = {
    keyword.strip()
    for keyword in os.getenv("SMART_HOME_KEYWORDS", "洗面所").split(",")
    if keyword.strip()
}
WAKE_ACK_ENABLED = os.getenv("WAKE_ACK_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
MIC_DEVICE_INDEX_RAW = os.getenv("MIC_DEVICE_INDEX", "").strip()
MIC_DEVICE_INDEX = int(MIC_DEVICE_INDEX_RAW) if MIC_DEVICE_INDEX_RAW else None
MIC_GAIN = float(os.getenv("MIC_GAIN", "1.0"))
MIN_RECORD_SECONDS = float(os.getenv("MIN_RECORD_SECONDS", "0.7"))
MIN_AUDIO_RMS = float(os.getenv("MIN_AUDIO_RMS", "0.006"))
LAST_WAV_PATH = os.getenv("LAST_WAV_PATH", "/tmp/voice-listener-last.wav").strip()
COMMAND_COOLDOWN_SECONDS = float(os.getenv("COMMAND_COOLDOWN_SECONDS", "8.0"))
WAKE_CONFIRM_CHUNKS = int(os.getenv("WAKE_CONFIRM_CHUNKS", "2"))
WAKE_RESET_THRESHOLD = float(os.getenv("WAKE_RESET_THRESHOLD", str(WAKE_THRESHOLD * 0.75)))
WAKE_DEBOUNCE_SECONDS = float(os.getenv("WAKE_DEBOUNCE_SECONDS", "10.0"))
REPEAT_COMMAND_IGNORE_SECONDS = float(os.getenv("REPEAT_COMMAND_IGNORE_SECONDS", "25.0"))
REQUIRE_DIFFERENT_COMMAND_SECONDS = float(os.getenv("REQUIRE_DIFFERENT_COMMAND_SECONDS", "90.0"))

SAMPLE_RATE = 16000
CHANNELS = 1

# openWakeWordは80ms程度のchunkで扱いやすい
WAKE_CHUNK_SAMPLES = 1280  # 16kHz * 0.08 sec

# VADは20ms frameで使う
VAD_FRAME_MS = 20
VAD_FRAME_SAMPLES = int(SAMPLE_RATE * VAD_FRAME_MS / 1000)

# 発話終了判定
MAX_RECORD_SECONDS = 12
START_PADDING_SECONDS = 0.4
END_SILENCE_SECONDS = 0.9

IGNORED_TRANSCRIPTS = {
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "最後までご視聴いただきありがとうございます",
    "最後までご視聴いただきありがとうございます。",
}

RESULT_PHRASE_RE = re.compile(r"(しました|できました|完了しました|つけました|点けました|消しました)")
TURN_ON_WORDS = ("つけ", "点け", "付け", "オン", "ON")
TURN_OFF_WORDS = ("消し", "けし", "オフ", "OFF")

audio_q: queue.Queue[np.ndarray] = queue.Queue()
audio_remainder = np.array([], dtype=np.float32)


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


def speak_status(text: str, emotion: str = "neutral"):
    """AITuberKitに短いステータスを喋らせる。失敗しても待受は止めない。"""
    try:
        requests.post(
            f"{INTERACTION_BRIDGE_URL}/speak",
            json={"text": text, "emotion": emotion},
            timeout=3,
        )
    except Exception as e:
        print(f"[warn] speak_status failed: {e}")


def send_text_to_openclaw(text: str):
    res = requests.post(
        f"{INTERACTION_BRIDGE_URL}/send-text",
        json={"text": text},
        timeout=30,
    )
    res.raise_for_status()
    return res.json()


def get_accepted_prediction(prediction: dict[str, float]) -> tuple[str, float] | None:
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


def transcribe_wav(path: str) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)
    with open(path, "rb") as f:
        result = client.audio.transcriptions.create(
            model=STT_MODEL,
            file=f,
            language="ja",
            prompt="スマートホームの短い日本語命令です。候補は「洗面所の電気をつけて」または「洗面所の電気を消して」です。",
            temperature=0,
        )
    return result.text.strip()


def normalize_command_text(text: str) -> str:
    normalized = text.strip()
    normalized = re.sub(r"^[、。,. !?！？\s]+", "", normalized)

    if "洗面所" in normalized:
        if any(word in normalized for word in TURN_ON_WORDS):
            return "洗面所の電気をつけて"
        if any(word in normalized for word in TURN_OFF_WORDS):
            return "洗面所の電気を消して"

    return normalized


def should_ignore_transcript(text: str) -> bool:
    if not text or text in IGNORED_TRANSCRIPTS:
        return True

    if SMART_HOME_KEYWORDS and not any(keyword in text for keyword in SMART_HOME_KEYWORDS):
        return True

    # AITuber/OpenClawの完了発話を拾ったものは命令として送らない。
    if "洗面所" in text and RESULT_PHRASE_RE.search(text):
        return True

    if "洗面所" in text and not any(word in text for word in TURN_ON_WORDS + TURN_OFF_WORDS):
        return True

    return False


def is_recent_repeat(
    command_text: str,
    last_command: str,
    last_command_time: float | None,
    repeat_seconds: float = REPEAT_COMMAND_IGNORE_SECONDS,
) -> bool:
    if not last_command_time:
        return False
    if command_text != last_command:
        return False
    return time.monotonic() - last_command_time < repeat_seconds


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


def record_utterance() -> np.ndarray:
    """
    Wake Word検出後、VADで発話区間だけ録音する。
    発話開始前の少しの音もpaddingとして残す。
    """
    vad = webrtcvad.Vad(2)  # 0-3。大きいほど厳しめ

    max_frames = int(MAX_RECORD_SECONDS * 1000 / VAD_FRAME_MS)
    end_silence_frames = int(END_SILENCE_SECONDS * 1000 / VAD_FRAME_MS)
    padding_frames = int(START_PADDING_SECONDS * 1000 / VAD_FRAME_MS)

    ring_buffer: list[np.ndarray] = []
    recorded: list[np.ndarray] = []

    triggered = False
    silence_count = 0

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


def main():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")

    print(f"[init] loading openWakeWord model ({OPENWAKEWORD_INFERENCE_FRAMEWORK})...")
    wake_model = Model(inference_framework=OPENWAKEWORD_INFERENCE_FRAMEWORK)
    print(f"[init] accepted wake words: {', '.join(sorted(ACCEPT_WAKE_WORDS)) or 'all'}")
    print(f"[init] stt model: {STT_MODEL}")

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
        last_command = ""
        last_command_time: float | None = None

        while True:
            # Wake Word用chunk取得
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

            if wake_confirm_count >= WAKE_CONFIRM_CHUNKS:
                print(
                    f"[wake] detected: {wake_candidate_name} "
                    f"score={wake_candidate_score:.3f} confirms={wake_confirm_count}"
                )
                wake_candidate_name = ""
                wake_candidate_score = 0.0
                wake_confirm_count = 0

                # Wakeモデルの内部状態だけリセットする。ここで音声キューを捨てると、
                # 「ヘイジャービス、洗面所...」の命令冒頭まで欠けることがある。
                reset_wake_model(wake_model)

                if WAKE_ACK_ENABLED:
                    speak_status("はい、聞いています。", "neutral")
                    time.sleep(1.2)
                    reset_wake_state(wake_model)

                utterance = record_utterance()
                duration, rms, peak = get_audio_stats(utterance)
                print(f"[audio] duration={duration:.2f}s rms={rms:.4f} peak={peak:.4f}")

                with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                    wav_path = tmp.name

                write_wav(wav_path, utterance)
                if LAST_WAV_PATH:
                    shutil.copyfile(wav_path, LAST_WAV_PATH)
                    print(f"[debug] saved last wav: {LAST_WAV_PATH}")

                if duration < MIN_RECORD_SECONDS:
                    print("[warn] utterance too short")
                    speak_status("もう一度お願いします。", "sad")
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass
                    continue

                if rms < MIN_AUDIO_RMS:
                    print("[warn] utterance too quiet; skip transcription")
                    speak_status("聞き取れませんでした。もう一度お願いします。", "sad")
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass
                    continue

                try:
                    print("[state] transcribing...")
                    text = transcribe_wav(wav_path)
                    print(f"[stt] {text}")

                    if should_ignore_transcript(text):
                        print("[warn] ignored non-command transcription")
                        speak_status("聞き取れませんでした。もう一度お願いします。", "sad")
                        continue

                    command_text = normalize_command_text(text)
                    if command_text != text:
                        print(f"[normalize] {command_text}")

                    if is_recent_repeat(command_text, last_command, last_command_time):
                        print("[warn] ignored recent repeated command")
                        continue

                    if is_recent_repeat(
                        command_text,
                        last_command,
                        last_command_time,
                        REQUIRE_DIFFERENT_COMMAND_SECONDS,
                    ):
                        print("[warn] ignored repeated command during post-action lockout")
                        continue

                    print("[state] sending to OpenClaw...")
                    result = send_text_to_openclaw(command_text)
                    print(f"[openclaw] {result}")
                    last_command = command_text
                    last_command_time = time.monotonic()
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
