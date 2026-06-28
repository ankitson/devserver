#!/usr/bin/env python3
"""Probe OpenAI-compatible Chat Completions and Responses API behavior.

This is intentionally dependency-free so it can run on the Linux host, the
Windows box, or inside a simple container.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


DEFAULT_PROMPT = (
    "Think briefly, then answer with exactly one short sentence. "
    "What is 3 + 5?"
)


@dataclass(frozen=True)
class Target:
    name: str
    base_url: str
    model: str
    api_key: str | None


def normalize_base_url(raw_url: str, target: str) -> str:
    url = raw_url.rstrip("/")
    path = urlsplit(url).path.rstrip("/")
    if path.endswith("/v1") or path.endswith("/openai/v1"):
        return url
    if target == "bifrost":
        return f"{url}/openai/v1"
    return f"{url}/v1"


def parse_bool_list(value: str) -> list[bool]:
    if value == "both":
        return [False, True]
    if value in {"true", "1", "yes", "stream"}:
        return [True]
    if value in {"false", "0", "no", "nonstream", "non-stream"}:
        return [False]
    raise argparse.ArgumentTypeError("expected true, false, or both")


def parse_api_list(value: str) -> list[str]:
    if value == "both":
        return ["chat", "responses"]
    parts = [part.strip() for part in value.split(",") if part.strip()]
    bad = [part for part in parts if part not in {"chat", "responses"}]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown API type(s): {', '.join(bad)}")
    return parts


def make_payload(api_type: str, model: str, stream: bool, args: argparse.Namespace) -> dict[str, Any]:
    if api_type == "chat":
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": args.prompt}],
            "max_tokens": args.max_tokens,
            "stream": stream,
        }
    else:
        payload = {
            "model": model,
            "input": args.prompt,
            "max_output_tokens": args.max_tokens,
            "stream": stream,
        }

    if args.enable_thinking != "omit":
        payload["enable_thinking"] = args.enable_thinking == "true"
    if args.extra_json:
        try:
            extra = json.loads(args.extra_json)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--extra-json is not valid JSON: {exc}") from exc
        if not isinstance(extra, dict):
            raise SystemExit("--extra-json must decode to a JSON object")
        payload.update(extra)
    return payload


def request_once(
    target: Target,
    api_type: str,
    stream: bool,
    args: argparse.Namespace,
) -> tuple[int | None, str, bytes, float]:
    endpoint = "chat/completions" if api_type == "chat" else "responses"
    url = f"{target.base_url}/{endpoint}"
    payload = make_payload(api_type, target.model, stream, args)
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if target.api_key:
        headers["Authorization"] = f"Bearer {target.api_key}"

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            content_type = resp.headers.get("content-type", "")
            if stream or "text/event-stream" in content_type.lower():
                body = read_stream_body(resp, args.max_bytes)
            else:
                body = resp.read(args.max_bytes)
            return resp.status, content_type, body, time.monotonic() - started
    except urllib.error.HTTPError as exc:
        body = exc.read(args.max_bytes)
        content_type = exc.headers.get("content-type", "")
        return exc.code, content_type, body, time.monotonic() - started
    except urllib.error.URLError as exc:
        return None, "transport-error", str(exc).encode("utf-8"), time.monotonic() - started


def read_stream_body(resp: Any, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0

    while total < max_bytes:
        try:
            line = resp.readline()
        except (TimeoutError, socket.timeout):
            chunks.append(b": client_read_timeout\n\n")
            break
        if not line:
            break
        chunks.append(line)
        total += len(line)
        stripped = line.strip()
        if stripped in {b"data: [DONE]", b"data:[DONE]"}:
            break

    if total >= max_bytes:
        chunks.append(b"\n: client_max_bytes_reached\n\n")
    return b"".join(chunks)


def iter_sse_events(body: bytes) -> list[tuple[str | None, str]]:
    text = body.decode("utf-8", errors="replace")
    events: list[tuple[str | None, str]] = []
    event_name: str | None = None
    data_lines: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                events.append((event_name, "\n".join(data_lines)))
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())

    if data_lines:
        events.append((event_name, "\n".join(data_lines)))
    return events


def extract_text_bits(api_type: str, obj: dict[str, Any]) -> tuple[str, str]:
    content: list[str] = []
    reasoning: list[str] = []

    if api_type == "chat":
        for choice in obj.get("choices", []) or []:
            message = choice.get("message") or {}
            delta = choice.get("delta") or {}
            for source in (message, delta):
                for key in ("content", "text"):
                    value = source.get(key)
                    if isinstance(value, str):
                        content.append(value)
                for key in ("reasoning_content", "reasoning", "reasoning_text"):
                    value = source.get(key)
                    if isinstance(value, str):
                        reasoning.append(value)
    else:
        for item in obj.get("output", []) or []:
            for part in item.get("content", []) or []:
                value = part.get("text")
                if isinstance(value, str):
                    if part.get("type") in {"reasoning_text", "reasoning"}:
                        reasoning.append(value)
                    else:
                        content.append(value)
        for key in ("output_text", "text"):
            value = obj.get(key)
            if isinstance(value, str):
                content.append(value)

    return "".join(content), "".join(reasoning)


def summarize_json(api_type: str, body: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return {"json_error": str(exc), "preview": preview(body)}

    content, reasoning = extract_text_bits(api_type, obj)
    return {
        "object": obj.get("object"),
        "model": obj.get("model"),
        "error": obj.get("error"),
        "content_preview": trim(content),
        "reasoning_preview": trim(reasoning),
        "raw_preview": trim(json.dumps(obj, ensure_ascii=False)),
    }


def summarize_sse(api_type: str, body: bytes, max_events: int) -> dict[str, Any]:
    events = iter_sse_events(body)
    event_names: list[str] = []
    content: list[str] = []
    reasoning: list[str] = []
    raw_samples: list[str] = []

    for event_name, data in events:
        if data == "[DONE]":
            event_names.append(event_name or "message")
            continue
        event_names.append(event_name or "message")
        if len(raw_samples) < max_events:
            raw_samples.append(f"{event_name or 'message'}: {trim(data, 240)}")
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue

        if api_type == "responses":
            response_type = obj.get("type") or event_name
            delta = obj.get("delta")
            if response_type in {"response.reasoning_text.delta", "response.reasoning.delta"}:
                if isinstance(delta, str):
                    reasoning.append(delta)
            elif response_type in {"response.output_text.delta", "response.text.delta"}:
                if isinstance(delta, str):
                    content.append(delta)
        else:
            c, r = extract_text_bits(api_type, obj)
            content.append(c)
            reasoning.append(r)

    return {
        "events": len(events),
        "event_names": compact_counts(event_names),
        "content_preview": trim("".join(content)),
        "reasoning_preview": trim("".join(reasoning)),
        "samples": raw_samples,
    }


def compact_counts(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def trim(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def preview(body: bytes, limit: int = 1000) -> str:
    return trim(body.decode("utf-8", errors="replace"), limit)


def print_result(
    target: Target,
    api_type: str,
    requested_stream: bool,
    status: int | None,
    content_type: str,
    body: bytes,
    elapsed: float,
    args: argparse.Namespace,
) -> None:
    print(f"\n== {target.name} | {api_type} | stream={str(requested_stream).lower()} ==")
    print(f"url: {target.base_url}")
    print(f"model: {target.model}")
    print(f"status: {status}  elapsed: {elapsed:.2f}s  content-type: {content_type or '-'}")

    lowered_type = content_type.lower()
    looks_sse = "text/event-stream" in lowered_type or body.lstrip().startswith(b"data:")
    if not requested_stream and looks_sse:
        print("note: server returned SSE even though stream=false")
    if b"client_read_timeout" in body:
        print("note: client read timed out before the stream sent [DONE]")
    if b"client_max_bytes_reached" in body:
        print("note: client stopped after --max-bytes")

    if status is None:
        print(f"transport_error: {preview(body)}")
    elif looks_sse:
        summary = summarize_sse(api_type, body, args.max_events)
        print(f"events: {summary['events']}  event_names: {summary['event_names'] or '-'}")
        if summary["reasoning_preview"]:
            print(f"reasoning: {summary['reasoning_preview']}")
        if summary["content_preview"]:
            print(f"content: {summary['content_preview']}")
        if args.show_samples:
            for sample in summary["samples"]:
                print(f"sample: {sample}")
    elif "json" in lowered_type or body.lstrip().startswith((b"{", b"[")):
        summary = summarize_json(api_type, body)
        if summary.get("error"):
            print(f"error: {json.dumps(summary['error'], ensure_ascii=False)}")
        if summary.get("reasoning_preview"):
            print(f"reasoning: {summary['reasoning_preview']}")
        if summary.get("content_preview"):
            print(f"content: {summary['content_preview']}")
        if args.show_samples:
            print(f"raw: {summary['raw_preview']}")
        elif summary.get("json_error"):
            print(f"json_error: {summary['json_error']}")
            print(f"raw: {summary['preview']}")
    else:
        print(f"body: {preview(body)}")


def build_targets(args: argparse.Namespace) -> list[Target]:
    configured = {
        "studio": Target(
            "studio",
            normalize_base_url(args.studio_url, "studio"),
            args.studio_model,
            args.studio_key or None,
        ),
        "bifrost": Target(
            "bifrost",
            normalize_base_url(args.bifrost_url, "bifrost"),
            args.bifrost_model,
            args.bifrost_key or None,
        ),
    }

    if args.llama_url:
        configured["llama"] = Target(
            "llama",
            normalize_base_url(args.llama_url, "llama"),
            args.llama_model,
            args.llama_key or None,
        )

    names = args.target
    if names == ["all"]:
        names = ["studio", "bifrost"] + (["llama"] if "llama" in configured else [])

    missing = [name for name in names if name not in configured]
    if missing:
        raise SystemExit(
            "missing target config for "
            + ", ".join(missing)
            + ". For direct llama, pass --llama-url or set LLAMA_BASE_URL."
        )
    return [configured[name] for name in names]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test chat/responses + streaming/non-streaming across Studio, Bifrost, and llama-server.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--target", action="append", choices=["all", "studio", "bifrost", "llama"], default=None)
    parser.add_argument("--api", type=parse_api_list, default=parse_api_list("both"), help="chat, responses, both, or comma list")
    parser.add_argument("--stream", type=parse_bool_list, default=parse_bool_list("both"), help="true, false, or both")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--max-tokens", type=int, default=160)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--max-bytes", type=int, default=1_000_000)
    parser.add_argument("--max-events", type=int, default=8)
    parser.add_argument("--show-samples", action="store_true")
    parser.add_argument("--enable-thinking", choices=["omit", "true", "false"], default="omit")
    parser.add_argument("--extra-json", help="JSON object merged into every request payload")

    parser.add_argument("--studio-url", default=os.getenv("UNSLOTH_STUDIO_URL", "http://desktop-win:8888"))
    parser.add_argument("--studio-model", default=os.getenv("UNSLOTH_MODEL", "unsloth/default"))
    parser.add_argument("--studio-key", default=os.getenv("UNSLOTH_STUDIO_API_KEY"))

    parser.add_argument("--bifrost-url", default=os.getenv("BIFROST_OPENAI_URL", os.getenv("BIFROST_URL", "http://127.0.0.1:8090")))
    parser.add_argument("--bifrost-model", default=os.getenv("BIFROST_MODEL", "unsloth/default"))
    parser.add_argument("--bifrost-key", default=os.getenv("BIFROST_API_KEY"))

    parser.add_argument("--llama-url", default=os.getenv("LLAMA_BASE_URL"))
    parser.add_argument("--llama-model", default=os.getenv("LLAMA_MODEL", "default"))
    parser.add_argument("--llama-key", default=os.getenv("LLAMA_API_KEY"))

    args = parser.parse_args()
    args.target = args.target or ["all"]
    return args


def main() -> int:
    args = parse_args()
    targets = build_targets(args)
    failures = 0

    for target in targets:
        for api_type in args.api:
            for stream in args.stream:
                status, content_type, body, elapsed = request_once(target, api_type, stream, args)
                print_result(target, api_type, stream, status, content_type, body, elapsed, args)
                if status is None or status >= 400:
                    failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
