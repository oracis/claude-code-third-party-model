#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ccproxy - a minimal, zero-dependency Anthropic <-> OpenAI translation gateway.

Why this exists:
    claude-code-router 2.1.1 emits malformed Anthropic SSE (every event is
    written twice and `message_stop` is never sent), which makes Claude Code
    report "The response stream was malformed". Verified by feeding it a
    textbook-perfect OpenAI SSE stream: still malformed. So we replace it.

What it does:
    POST /v1/messages   (Anthropic Messages API, what Claude Code speaks)
      -> translated into an OpenAI /chat/completions request
      -> sent upstream with http.client (ignores HTTP_PROXY entirely)
      -> translated back, with a strictly spec-shaped Anthropic event stream
         (message_start / content_block_* / message_delta / message_stop).

Config: ~/.claude-code-proxy.json (see DEFAULT_CONFIG below)

Usage: python ccproxy.py [--port N] [--upstream URL] [--model NAME]
"""
import argparse
import http.client
import json
import os
import ssl
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".claude-code-proxy.json")

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 3457,
    "upstream": {
        "url": "https://opencode.ai/zen/v1/chat/completions",
        "model": "space-bunny-free",
        "api_key": "",
    },
    "drop_reasoning": True,
    "timeout": 900,
    "verbose": False,
}

STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}

VALID_STOP = {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"}


def log(*a):
    if CONFIG.get("verbose"):
        sys.stderr.write("[ccproxy] " + " ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            sys.stderr.write("[ccproxy] bad config %s: %s\n" % (CONFIG_PATH, e))
    return cfg


CONFIG = {}


# --------------------------------------------------------------- request side
def _text_of(content):
    """Flatten an Anthropic content value into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                parts.append(str(b))
                continue
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "tool_result":
                parts.append(_text_of(b.get("content")))
            elif t == "image":
                parts.append("[image omitted]")
            else:
                parts.append(json.dumps(b, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def anthropic_to_openai(req):
    """Anthropic Messages request -> OpenAI chat.completions request."""
    msgs = []

    system = req.get("system")
    if system:
        msgs.append({"role": "system", "content": _text_of(system)})

    for m in req.get("messages", []):
        role = m.get("role")
        content = m.get("content")

        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            msgs.append({"role": role, "content": _text_of(content)})
            continue

        # A user turn may carry tool_result blocks; OpenAI wants those as
        # separate role="tool" messages, so we split them out in order.
        texts, tool_results, tool_calls = [], [], []
        for b in content:
            if not isinstance(b, dict):
                texts.append(str(b))
                continue
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
            elif t == "tool_result":
                tool_results.append(b)
            elif t == "tool_use":
                tool_calls.append(b)
            elif t == "image":
                texts.append("[image omitted]")

        joined = "\n".join(x for x in texts if x)

        if tool_results:
            if joined:
                msgs.append({"role": role, "content": joined})
            for tr in tool_results:
                body = tr.get("content")
                out = _text_of(body)
                if tr.get("is_error"):
                    out = "ERROR: " + out
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id") or "unknown",
                    "content": out or "(empty)",
                })
            continue

        if tool_calls:
            oa_calls = []
            for tc in tool_calls:
                args = tc.get("input")
                oa_calls.append({
                    "id": tc.get("id") or ("call_" + uuid.uuid4().hex[:20]),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(args if args is not None else {},
                                                ensure_ascii=False),
                    },
                })
            msg = {"role": "assistant", "tool_calls": oa_calls}
            if joined:
                msg["content"] = joined
            msgs.append(msg)
            continue

        msgs.append({"role": role, "content": joined})

    out = {
        "model": CONFIG["upstream"].get("model", "unknown"),
        "messages": msgs,
        "stream": bool(req.get("stream")),
    }

    if req.get("max_tokens"):
        out["max_tokens"] = req["max_tokens"]
    for k in ("temperature", "top_p"):
        if req.get(k) is not None:
            out[k] = req[k]
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]

    tools = req.get("tools")
    if tools:
        oa_tools = []
        for t in tools:
            oa_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        out["tools"] = oa_tools
        tc = req.get("tool_choice") or {}
        ct = tc.get("type")
        if ct == "any":
            out["tool_choice"] = "required"
        elif ct == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif ct == "none":
            out["tool_choice"] = "none"
        else:
            out["tool_choice"] = "auto"

    # Reasoning hints: map Anthropic's thinking budget onto reasoning_effort.
    th = req.get("thinking") or {}
    if isinstance(th, dict) and th.get("type") == "enabled":
        budget = th.get("budget_tokens") or 0
        out["reasoning_effort"] = "high" if budget >= 8000 else (
            "medium" if budget >= 2000 else "low")

    return out


def estimate_tokens(text):
    if not text:
        return 0
    return max(1, int(len(text) / 3.5))


# -------------------------------------------------------------- response side
def upstream_request(oa_req):
    u = urlparse(CONFIG["upstream"]["url"])
    timeout = CONFIG.get("timeout", 900)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(
            u.hostname, u.port or 443, timeout=timeout,
            context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if oa_req.get("stream") else "application/json",
    }
    key = CONFIG["upstream"].get("api_key") or ""
    if key:
        headers["Authorization"] = "Bearer " + key
    body = json.dumps(oa_req, ensure_ascii=False).encode("utf-8")
    conn.request("POST", path, body=body, headers=headers)
    return conn, conn.getresponse()


def openai_to_anthropic(resp, model):
    """Non-streaming OpenAI completion -> Anthropic message."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []

    text = msg.get("content")
    if text:
        content.append({"type": "text", "text": text})

    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {"_raw": fn.get("arguments")}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:20]),
            "name": fn.get("name", ""),
            "input": args,
        })

    if not content:
        content.append({"type": "text", "text": ""})

    fr = choice.get("finish_reason") or "stop"
    usage = resp.get("usage") or {}
    return {
        "id": "msg_" + uuid.uuid4().hex[:24],
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": STOP_MAP.get(fr, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


class StreamTranslator(object):
    """Turn an OpenAI SSE stream into a strictly-shaped Anthropic event stream.

    Invariants enforced (the exact things claude-code-router got wrong):
      * exactly one message_start, and it is the first event
      * content blocks are opened, filled and closed in index order
      * exactly one message_delta carrying a real stop_reason
      * exactly one message_stop, always emitted, even on upstream failure
      * every event is written once
    """

    def __init__(self, model, input_chars):
        self.model = model
        self.msg_id = "msg_" + uuid.uuid4().hex[:24]
        self.input_tokens = estimate_tokens(" " * input_chars)
        self.out_chars = 0
        self.blocks = []          # list of {"idx":int,"kind":"text"|"tool","open":bool}
        self.text_idx = None
        self.tool_by_slot = {}    # upstream tool_call index -> our block index
        self.stop_reason = "end_turn"
        self.started = False
        self.finished = False

    # -- low level ---------------------------------------------------------
    @staticmethod
    def ev(name, data):
        return ("event: %s\ndata: %s\n\n" % (name, json.dumps(data, ensure_ascii=False))
                ).encode("utf-8")

    def start(self):
        self.started = True
        return self.ev("message_start", {
            "type": "message_start",
            "message": {
                "id": self.msg_id, "type": "message", "role": "assistant",
                "model": self.model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
            },
        })

    def _open_text(self):
        if self.text_idx is not None:
            return b""
        idx = len(self.blocks)
        self.text_idx = idx
        self.blocks.append({"idx": idx, "kind": "text", "open": True})
        return self.ev("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "text", "text": ""},
        })

    def _close(self, idx):
        for b in self.blocks:
            if b["idx"] == idx and b["open"]:
                b["open"] = False
                return self.ev("content_block_stop",
                               {"type": "content_block_stop", "index": idx})
        return b""

    def close_open_blocks(self):
        out = b""
        for b in self.blocks:
            if b["open"]:
                out += self._close(b["idx"])
        return out

    # -- per-chunk ---------------------------------------------------------
    def feed(self, chunk):
        out = b""
        choices = chunk.get("choices") or []
        if not choices:
            return out
        ch = choices[0]
        delta = ch.get("delta") or {}

        text = delta.get("content")
        if text:
            if self.text_idx is None:
                out += self._open_text()
            self.out_chars += len(text)
            out += self.ev("content_block_delta", {
                "type": "content_block_delta", "index": self.text_idx,
                "delta": {"type": "text_delta", "text": text},
            })

        for tc in (delta.get("tool_calls") or []):
            slot = tc.get("index")
            if slot is None:
                slot = len(self.tool_by_slot)
            fn = tc.get("function") or {}

            if slot not in self.tool_by_slot:
                # Do NOT close other open blocks here: upstreams interleave
                # argument deltas across parallel tool calls, so every block
                # stays open until the stream ends (they are closed in index
                # order by finish()).
                idx = len(self.blocks)
                self.tool_by_slot[slot] = idx
                self.blocks.append({
                    "idx": idx, "kind": "tool", "open": True,
                    "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:20]),
                    "name": fn.get("name") or "",
                })
                blk = self.blocks[-1]
                out += self.ev("content_block_start", {
                    "type": "content_block_start", "index": idx,
                    "content_block": {
                        "type": "tool_use", "id": blk["id"],
                        "name": blk["name"], "input": {},
                    },
                })
            else:
                idx = self.tool_by_slot[slot]
                blk = self.blocks[idx] if idx < len(self.blocks) else None
                # Some upstreams send the name only on a later chunk.
                if blk and not blk.get("name") and fn.get("name"):
                    blk["name"] = fn["name"]

            args = fn.get("arguments")
            if args:
                out += self.ev("content_block_delta", {
                    "type": "content_block_delta", "index": self.tool_by_slot[slot],
                    "delta": {"type": "input_json_delta", "partial_json": args},
                })

        fr = ch.get("finish_reason")
        if fr:
            self.stop_reason = STOP_MAP.get(fr, "end_turn")

        return out

    # -- tail --------------------------------------------------------------
    def finish(self):
        if self.finished:
            return b""
        self.finished = True
        out = self.close_open_blocks()
        if self.text_idx is None and not self.blocks:
            # Upstream produced nothing usable: still emit a valid empty block
            # so the stream stays well-formed instead of "malformed".
            out += self._open_text() + self._close(0)
        if self.stop_reason not in VALID_STOP:
            self.stop_reason = "end_turn"
        out += self.ev("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": estimate_tokens(" " * self.out_chars)},
        })
        out += self.ev("message_stop", {"type": "message_stop"})
        return out


# -------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ccproxy/1.0"

    def log_message(self, fmt, *args):
        if CONFIG.get("verbose"):
            sys.stderr.write("[ccproxy] " + (fmt % args) + "\n")
            sys.stderr.flush()

    # -- helpers -----------------------------------------------------------
    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_anthropic(self, code, kind, message):
        self._send_json(code, {"type": "error",
                               "error": {"type": kind, "message": message}})

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/", "/status"):
            self._send_json(200, {
                "ok": True,
                "upstream": CONFIG["upstream"].get("url"),
                "model": CONFIG["upstream"].get("model"),
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/v1/messages":
            self._send_error_anthropic(404, "not_found_error", "unsupported path " + path)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send_error_anthropic(400, "invalid_request_error", "bad json: %s" % e)
            return

        want_stream = bool(req.get("stream"))
        try:
            oa_req = anthropic_to_openai(req)
        except Exception as e:
            self._send_error_anthropic(400, "invalid_request_error", "translate: %s" % e)
            return

        prompt_chars = sum(len(str(m.get("content", ""))) for m in oa_req["messages"])
        model_for_reply = req.get("model") or CONFIG["upstream"].get("model")

        conn = resp = None
        try:
            conn, resp = upstream_request(oa_req)
        except Exception as e:
            self._send_error_anthropic(502, "api_error", "upstream connect failed: %s" % e)
            if conn:
                conn.close()
            return

        try:
            if resp.status != 200:
                detail = resp.read().decode("utf-8", "replace")[:800]
                log("upstream", resp.status, detail)
                kind = "authentication_error" if resp.status in (401, 403) else "api_error"
                self._send_error_anthropic(resp.status, kind,
                                           "upstream %s: %s" % (resp.status, detail))
                return

            if not want_stream:
                data = json.loads(resp.read().decode("utf-8"))
                self._send_json(200, openai_to_anthropic(data, model_for_reply))
                return

            # ---- streaming ----
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            tr = StreamTranslator(model_for_reply, prompt_chars)
            self.wfile.write(tr.start())
            self.wfile.flush()

            for line in resp:
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                try:
                    chunk = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                out = tr.feed(chunk)
                if out:
                    self.wfile.write(out)
                    self.wfile.flush()

            self.wfile.write(tr.finish())
            self.wfile.flush()
        except Exception as e:
            log("stream error:", e)
            # Never leave the client hanging: try to close the stream cleanly.
            try:
                if want_stream and getattr(self, "wfile", None):
                    tr = locals().get("tr")
                    if tr is not None:
                        self.wfile.write(tr.finish())
                    else:
                        self.wfile.write(StreamTranslator(model_for_reply, 0).finish())
                    self.wfile.flush()
            except Exception:
                pass
        finally:
            try:
                if resp:
                    resp.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass


def main():
    global CONFIG
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--upstream")
    ap.add_argument("--model")
    ap.add_argument("--key")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    CONFIG = load_config()
    if args.port:
        CONFIG["port"] = args.port
    if args.host:
        CONFIG["host"] = args.host
    if args.upstream:
        CONFIG["upstream"]["url"] = args.upstream
    if args.model:
        CONFIG["upstream"]["model"] = args.model
    if args.key is not None:
        CONFIG["upstream"]["api_key"] = args.key
    if args.verbose:
        CONFIG["verbose"] = True

    host, port = CONFIG["host"], CONFIG["port"]
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    sys.stderr.write(
        "[ccproxy] listening on http://%s:%d  ->  %s (%s)\n"
        % (host, port, CONFIG["upstream"]["url"], CONFIG["upstream"]["model"]))
    if not CONFIG["upstream"].get("api_key"):
        sys.stderr.write("[ccproxy] upstream api_key is empty (keyless upstream)\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
