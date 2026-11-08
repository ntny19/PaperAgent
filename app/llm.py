"""Model backends. All CLIs are called headless, in an empty neutral folder, with their own tools disabled,
so the only tools the model can use are the ones PaperAgent exposes (which enforce the secret rules)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

NEUTRAL = Path(tempfile.gettempdir()) / "paperagent_neutral"
NEUTRAL.mkdir(exist_ok=True)


class LLMError(RuntimeError):
    pass


def _run(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, input=stdin_text.encode("utf-8"), capture_output=True, timeout=timeout, cwd=NEUTRAL)
    except subprocess.TimeoutExpired:
        raise LLMError(f"model timed out after {timeout}s")
    except FileNotFoundError as e:
        raise LLMError(f"CLI not found: {e}")
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def _agy_once(model: str, prompt: str, timeout: int) -> str:
    exe = shutil.which("agy") or "agy"
    cmd = [exe, "--model", model, "--mode", "plan", "--input-format", "stream-json",
           "--output-format", "stream-json", "-p="]
    msg = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
    rc, out, err = _run(cmd, msg, timeout)
    result = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "result":
            result = ev["result"]
    if not result:
        raise LLMError(f"agy returned no result (rc={rc}): {(err or out)[:400]}")
    if result.get("status") != "SUCCESS":
        raise LLMError(f"agy error: {result.get('error') or result.get('status')}")
    text = result.get("response", "")
    if not text.strip():
        raise LLMError(f"agy returned an empty response (rc={rc}). stdout tail: {out[-600:]!r} stderr: {err[-300:]!r}")
    return text


NO_TOOLS_REMINDER = (
    "\n\nREMINDER: you have NO built-in shell, file or browser tools in this session (they are disabled). "
    "Do not call run_command/view_file/etc. The ONLY way to act is to write the ```tool JSON block as plain text in your reply."
)


def _agy(model: str, prompt: str, timeout: int) -> str:
    last = None
    for attempt in range(3):
        try:
            return _agy_once(model, prompt + (NO_TOOLS_REMINDER * (attempt + 1) if attempt else ""), timeout)
        except LLMError as e:
            last = e
            if "empty response" not in str(e):
                raise
    raise last


def _codex(model: str, prompt: str, timeout: int) -> str:
    exe = shutil.which("codex.cmd") or shutil.which("codex") or "codex"
    fd, outfile = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        cmd = [exe, "exec", "--skip-git-repo-check", "-s", "read-only", "-o", outfile]
        if model:
            cmd += ["-m", model]
        cmd += ["-"]
        rc, out, err = _run(cmd, prompt, timeout)
        text = Path(outfile).read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            raise LLMError(f"codex returned nothing (rc={rc}): {(err or out)[-400:]}")
        return text
    finally:
        Path(outfile).unlink(missing_ok=True)


def _claude(model: str, prompt: str, timeout: int) -> str:
    exe = shutil.which("claude") or "claude"
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--tools", "", "--no-session-persistence"]
    rc, out, err = _run(cmd, prompt, timeout)
    try:
        data = json.loads(out)
    except ValueError:
        raise LLMError(f"claude returned invalid output (rc={rc}): {(err or out)[:400]}")
    if data.get("is_error"):
        raise LLMError(f"claude error: {data.get('result')}")
    return data.get("result", "")


def _ollama(model: str, prompt: str, timeout: int, url: str) -> str:
    r = httpx.post(
        f"{url}/api/chat",
        json={"model": model, "stream": False, "keep_alive": "10m",
              "options": {"num_ctx": 12288, "temperature": 0.2},
              "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise LLMError(f"ollama error {r.status_code}: {r.text[:300]}")
    return r.json()["message"]["content"]


async def complete(entry: dict, prompt: str, ollama_url: str, timeout: int = 900) -> str:
    backend, model = entry["backend"], entry.get("model", "")
    if backend == "agy":
        fn = lambda: _agy(model, prompt, timeout)
    elif backend == "codex":
        fn = lambda: _codex(model, prompt, timeout)
    elif backend == "claude":
        fn = lambda: _claude(model, prompt, timeout)
    elif backend == "ollama":
        fn = lambda: _ollama(model, prompt, timeout, ollama_url)
    else:
        raise LLMError(f"unknown backend {backend}")
    return await asyncio.to_thread(fn)
