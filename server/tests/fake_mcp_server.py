"""Крихітний MCP-сервер для тестів (stdio, JSON-RPC по рядках): echo, картинка, помилка, падіння."""

import base64
import json
import sys
from pathlib import Path

PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")).decode()
TOOLS = [
    {"name": "echo", "description": "Повторити текст", "inputSchema": {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "doc.preview", "description": "Прев'ю документа", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "Завжди помилка", "inputSchema": {"type": "object"}},
    {"name": "crash", "description": "Впасти", "inputSchema": {"type": "object"}},
]


if len(sys.argv) > 1 and (Path(sys.argv[1]) / "down").exists():  # «ПК вимкнений / програма не відкрита»
    sys.exit(1)


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if rid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": params["protocolVersion"],
                                                      "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}})
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})  # запит від сервера — клієнт має відповісти
    elif method == "tools/list":
        page = TOOLS[:2] if not params.get("cursor") else TOOLS[2:]
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": page, **({} if params.get("cursor") else {"nextCursor": "2"})}})
    elif method == "tools/call":
        name, args = params["name"], params.get("arguments") or {}
        if name == "echo":
            res = {"content": [{"type": "text", "text": f"ехо: {args.get('text')} {sorted(args)}"}]}
        elif name == "doc.preview":
            res = {"content": [{"type": "text", "text": "ось прев'ю"}, {"type": "image", "mimeType": "image/png", "data": PNG}]}
        elif name == "fail":
            res = {"content": [{"type": "text", "text": "документ не відкрито"}], "isError": True}
        else:
            sys.exit(3)
        send({"jsonrpc": "2.0", "id": rid, "result": res})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}})
