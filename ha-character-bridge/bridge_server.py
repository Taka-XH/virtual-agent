import asyncio
import base64
import hashlib
import json
import os
import signal
from typing import Set


HOST = os.getenv("HA_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.getenv("HA_BRIDGE_PORT", "8000"))
PATH = os.getenv("HA_BRIDGE_PATH", "/ws")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

clients: Set[asyncio.StreamWriter] = set()


async def read_request(reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("latin1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    return lines[0], headers


async def send_response(
    writer: asyncio.StreamWriter, status: str, body: str = ""
) -> None:
    payload = body.encode("utf-8")
    writer.write(
        (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        + payload
    )
    await writer.drain()


async def accept_websocket(
    writer: asyncio.StreamWriter, headers: dict[str, str]
) -> bool:
    key = headers.get("sec-websocket-key")
    if not key:
        await send_response(writer, "400 Bad Request", "Missing Sec-WebSocket-Key")
        return False

    accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest())
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept.decode('ascii')}\r\n"
            "\r\n"
        ).encode("ascii")
    )
    await writer.drain()
    return True


async def read_frame(reader: asyncio.StreamReader) -> str | None:
    header = await reader.readexactly(2)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F

    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")

    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

    if opcode == 0x8:
        return None
    if opcode != 0x1:
        return ""
    return payload.decode("utf-8")


async def send_text(writer: asyncio.StreamWriter, text: str) -> None:
    payload = text.encode("utf-8")
    header = bytearray([0x81])
    if len(payload) < 126:
        header.append(len(payload))
    elif len(payload) < 65536:
        header.append(126)
        header.extend(len(payload).to_bytes(2, "big"))
    else:
        header.append(127)
        header.extend(len(payload).to_bytes(8, "big"))
    writer.write(bytes(header) + payload)
    await writer.drain()


async def broadcast(message: str, sender: asyncio.StreamWriter) -> int:
    stale = []
    delivered = 0
    for client in clients:
        if client is sender:
            continue
        try:
            await send_text(client, message)
            delivered += 1
        except OSError:
            stale.append(client)
    for client in stale:
        clients.discard(client)
    return delivered


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        request_line, headers = await read_request(reader)
        parts = request_line.split()
        path = parts[1] if len(parts) >= 2 else ""
        if path != PATH:
            await send_response(writer, "404 Not Found", f"Use ws://{HOST}:{PORT}{PATH}")
            return
        if not await accept_websocket(writer, headers):
            return

        peer = writer.get_extra_info("peername")
        origin = headers.get("origin", "-")
        user_agent = headers.get("user-agent", "-")
        clients.add(writer)
        print(
            f"connected: {len(clients)} client(s), peer={peer}, "
            f"origin={origin}, user-agent={user_agent}"
        )

        while True:
            message = await read_frame(reader)
            if message is None:
                break
            if not message:
                continue

            json.loads(message)
            delivered = await broadcast(message, writer)
            print(f"broadcast to {delivered} client(s): {message}")
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    except json.JSONDecodeError as exc:
        print(f"invalid json: {exc}")
    finally:
        clients.discard(writer)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        print(f"disconnected: {len(clients)} client(s)")


async def main() -> None:
    server = await asyncio.start_server(handle_client, HOST, PORT)
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)

    print(f"listening on ws://{HOST}:{PORT}{PATH}")
    async with server:
        await stop


if __name__ == "__main__":
    asyncio.run(main())
