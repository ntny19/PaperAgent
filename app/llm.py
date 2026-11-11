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


def _add(usage: dict | None, **kv) -> None:
    """Accumulate real token counts reported by a backend (summed across retries)."""
    if usage is None:
        return
    for k, v in kv.items():
        usage[k] = usage.get(k, 0) + int(v or 0)
    usage["reported"] = True


def _run(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, input=stdin_text.encode("utf-8"), capture_output=True, timeout=timeout, cwd=NEUTRAL)
    except subprocess.TimeoutExpired:
        raise LLMError(f"model timed out after {timeout}s")
    except FileNotFoundError as e:
        raise LLMError(f"CLI not found: {e}")
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def _agy_once(model: str, prompt: str, timeout: int, usage: dict | None = None) -> str:
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
    u = result.get("usage") or {}
    _add(usage, tokens_in=u.get("input_tokens"), tokens_out=u.get("output_tokens"),
         thinking=u.get("thinking_tokens"), cache_read=u.get("cache_read_tokens"))
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


def _agy(model: str, prompt: str, timeout: int, usage: dict | None = None) -> str:
    last = None
    for attempt in range(3):
        try:
            return _agy_once(model, prompt + (NO_TOOLS_REMINDER * (attempt + 1) if attempt else ""), timeout, usage)
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


def _claude(model: str, prompt: str, timeout: int, usage: dict | None = None) -> str:
    exe = shutil.which("claude") or "claude"
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--tools", "", "--no-session-persistence"]
    rc, out, err = _run(cmd, prompt, timeout)
    try:
        data = json.loads(out)
    except ValueError:
        raise LLMError(f"claude returned invalid output (rc={rc}): {(err or out)[:400]}")
    u = data.get("usage") or {}
    cache_r, cache_w = u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens")
    _add(usage, tokens_in=(u.get("input_tokens") or 0) + (cache_r or 0) + (cache_w or 0), tokens_out=u.get("output_tokens"),
         thinking=(u.get("output_tokens_details") or {}).get("thinking_tokens"), cache_read=cache_r)
    if data.get("is_error"):
        raise LLMError(f"claude error: {data.get('result')}")
    return data.get("result", "")


def _ollama(model: str, prompt: str, timeout: int, url: str, usage: dict | None = None) -> str:
    r = httpx.post(
        f"{url}/api/chat",
        json={"model": model, "stream": False, "keep_alive": "10m",
              "options": {"num_ctx": 12288, "temperature": 0.2},
              "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise LLMError(f"ollama error {r.status_code}: {r.text[:300]}")
    j = r.json()
    _add(usage, tokens_in=j.get("prompt_eval_count"), tokens_out=j.get("eval_count"))
    return j["message"]["content"]


async def complete(entry: dict, prompt: str, ollama_url: str, timeout: int = 900, usage: dict | None = None) -> str:
    """Returns the reply text. If `usage` is given it is filled with tokens_in/tokens_out (+thinking, cache_read);
    "estimated": True means the backend reported nothing (codex) and the numbers are chars/4 guesses."""
    backend, model = entry["backend"], entry.get("model", "")
    if backend == "agy":
        fn = lambda: _agy(model, prompt, timeout, usage)
    elif backend == "codex":
        fn = lambda: _codex(model, prompt, timeout)
    elif backend == "claude":
        fn = lambda: _claude(model, prompt, timeout, usage)
    elif backend == "ollama":
        fn = lambda: _ollama(model, prompt, timeout, ollama_url, usage)
    else:
        raise LLMError(f"unknown backend {backend}")
    text = await asyncio.to_thread(fn)
    if usage is not None and not usage.pop("reported", False):
        usage.update(tokens_in=len(prompt) // 4, tokens_out=len(text) // 4, estimated=True)
    return text
