import json
import websocket

def speak(text: str, emotion: str = "happy"):
    ws = websocket.create_connection("ws://localhost:8000/ws")
    message = {
        "text": text,
        "role": "assistant",
        "emotion": emotion,
        "type": "message"
    }
    ws.send(json.dumps(message, ensure_ascii=False))
    ws.close()

speak("こんにちは。私はお家のAIアシスタントです。", "happy")
