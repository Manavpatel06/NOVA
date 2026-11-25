"""A tiny MCP server for tests (ADR 0030): newline JSON-RPC on stdin/stdout."""
import json
import os
import sys

TOOLS = [
    {"name": "search_mail", "description": "Search mail.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "what to find"},
         "limit": {"type": "integer"},
         "user_google_email": {"type": "string"}}, "required": ["query"]}},
    {"name": "draft_mail", "description": "Draft an email.",
     "annotations": {"readOnlyHint": False, "destructiveHint": False},
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                     "required": ["to", "body"]}},
    {"name": "send_gmail_message", "description": "Send an email.",
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}}},
]
PAGE2 = [
    {"name": "delete_event", "description": "Delete an event.", "annotations": {"destructiveHint": True},
     "inputSchema": {"type": "object", "properties": {"event_id": {"type": "string"},
                                                       "notify": {"type": "boolean"},
                                                       "tags": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "weird_params", "description": "Bad names.",
     "inputSchema": {"type": "object", "properties": {"user-name": {"type": "string"}}}},
    {"name": "fail", "description": "Always an error.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "env", "description": "Shows its environment.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "die", "description": "Exits.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "readfile", "description": "Reads a file.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


print("starting up", file=sys.stderr, flush=True)
sys.stdout.write("not json noise\n")
sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": params.get("protocolVersion"),
              "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"},
              "instructions": "fake server"}})
    elif method == "tools/list":
        if params.get("cursor") == "p2":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": PAGE2}})
        else:
            send({"jsonrpc": "2.0", "id": 999, "method": "ping"})  # a server->client request
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}})
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS, "nextCursor": "p2"}})
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "fail":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "no such thing"}],
                                                          "isError": True}})
        elif name == "die":
            sys.exit(3)
        elif name == "env":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": json.dumps({"HOME": os.environ.get("HOME"),
                                                     "TOKEN": os.environ.get("FAKE_TOKEN"),
                                                     "SECRET_OUTSIDE": os.environ.get("NOVA_TEST_OUTSIDE")})}]}})
        elif name == "readfile":
            try:
                text = open(args["path"]).read()
            except OSError as exc:
                text = f"denied: {type(exc).__name__}"
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
        elif name == "unknown":
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "Unknown tool"}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": f"{name}: {json.dumps(args, sort_keys=True)}"},
                {"type": "image", "data": "", "mimeType": "image/png"}]}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
