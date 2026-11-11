"""Turn tracing: one JSON line per chat turn in data/logs/YYYY-MM-DD.jsonl (data/ is git-ignored).

Each line holds the question, every step's prompt + raw model reply + tool call/result + timings + token counts
(tokens_in / tokens_out / thinking / cache_read per step and summed per turn; real numbers for claude, agy and ollama,
chars/4 estimates - flagged "estimated" - for codex), and the final answer. Everything is passed through mask_text(), so no secret reaches the log. View with:
    python -m app.logview

Env: PAPERAGENT_LOG=0 disables logging; PAPERAGENT_LOG_FULL_PROMPT=1 stores the whole prompt each step
(default stores only the part after the system prompt, plus its hash, to keep the files small).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from .secrets_guard import mask_text

ROOT = Path(__file__).resolve().parent.parent
MARK = "# CONVERSATION\n"


def log_dir() -> Path:
    return Path(os.environ.get("PAPERAGENT_DATA_DIR") or ROOT / "data") / "logs"


class TurnLog:
    def __init__(self, model_entry: dict, question: str, workspace: dict | None, tag: str | None = None):
        self.enabled = os.environ.get("PAPERAGENT_LOG", "1") != "0"
        self.full = os.environ.get("PAPERAGENT_LOG_FULL_PROMPT") == "1"
        self.rec = {
            "id": uuid.uuid4().hex[:8],
            "ts": datetime.now().isoformat(timespec="seconds"),
            "tag": tag,
            "model": model_entry.get("id"),
            "backend": model_entry.get("backend"),
            "workspace": (workspace or {}).get("name"),
            "question": mask_text(question),
            "steps": [],
        }
        self.t0 = time.time()

    def step(self, n: int, prompt: str, reply: str, llm_s: float, call: dict | None = None,
             result: str | None = None, tool_s: float | None = None, usage: dict | None = None) -> None:
        head, sep, tail = prompt.partition(MARK)
        shown = prompt if (self.full or not sep) else MARK + tail
        s = {
            "n": n,
            "prompt_chars": len(prompt),
            "system_sha": hashlib.sha1(head.encode()).hexdigest()[:8],
            "prompt": mask_text(shown),
            "reply": mask_text(reply),
            "llm_s": round(llm_s, 2),
            "tokens_in": (usage or {}).get("tokens_in", 0),
            "tokens_out": (usage or {}).get("tokens_out", 0),
            "thinking": (usage or {}).get("thinking", 0),
            "cache_read": (usage or {}).get("cache_read", 0),
            "estimated": bool((usage or {}).get("estimated")),
        }
        if call:
            s["tool"] = {"name": call["name"], "args": call["args"]}
            s["tool_result"] = mask_text(result or "")
            s["tool_s"] = round(tool_s or 0, 2)
        self.rec["steps"].append(s)

    def end(self, answer: str | None, error: str | None = None) -> None:
        st = self.rec["steps"]
        self.rec.update(answer=answer, error=error, total_s=round(time.time() - self.t0, 2),
                        tokens_in=sum(s["tokens_in"] for s in st), tokens_out=sum(s["tokens_out"] for s in st),
                        thinking=sum(s["thinking"] for s in st), tokens_estimated=any(s["estimated"] for s in st),
                        tool_calls=[s["tool"]["name"] for s in self.rec["steps"] if "tool" in s])
        if not self.enabled:
            return
        try:
            d = log_dir()
            d.mkdir(parents=True, exist_ok=True)
            with open(d / f"{datetime.now():%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(self.rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break a chat turn
