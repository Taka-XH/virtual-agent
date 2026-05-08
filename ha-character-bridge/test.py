import websocket
import json

WS_URL = "ws://localhost:8000/ws"

def send_message(text, emotion="neutral"):
    ws = websocket.create_connection(WS_URL)
    message = {
        "text": text,
        "role": "assistant",
        "emotion": emotion,
        "type": "message"
    }
    payload = json.dumps(message, ensure_ascii=False)
    ws.send(payload)
    ws.close()
    print(f"sent to {WS_URL}: {payload}")

send_message("こんにちは！元気ですか？", "happy")
