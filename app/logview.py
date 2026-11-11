"""Readable view of the turn logs.   python -m app.logview [-n 5] [--full] [--tag eval] [--file path]"""
import argparse
import json
from pathlib import Path

from .trace import log_dir


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3, help="how many latest turns")
    ap.add_argument("--full", action="store_true", help="print full prompts and tool results")
    ap.add_argument("--tag", help="only turns whose tag starts with this (e.g. eval)")
    ap.add_argument("--file", help="a specific .jsonl (default: newest in data/logs)")
    a = ap.parse_args()
    files = [Path(a.file)] if a.file else sorted(log_dir().glob("*.jsonl"))
    if not files:
        raise SystemExit(f"no logs in {log_dir()} yet")
    turns = [json.loads(l) for f in files[-2:] for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.tag:
        turns = [t for t in turns if (t.get("tag") or "").startswith(a.tag)]
    clip = (lambda s: s) if a.full else (lambda s: s if len(s) < 700 else s[:700] + f" ...[+{len(s) - 700} chars]")
    for t in turns[-a.n:]:
        print("=" * 100)
        print(f"{t['ts']}  id={t['id']}  model={t['model']}  ws={t['workspace']}  tag={t.get('tag')}  "
              f"{t['total_s']}s  tokens in/out={t.get('tokens_in', 0)}/{t.get('tokens_out', 0)}"
              f"{'~' if t.get('tokens_estimated') else ''}  tools={t['tool_calls']}")
        print(f"Q: {t['question']}")
        for s in t["steps"]:
            print(f"--- step {s['n']}  llm {s['llm_s']}s  prompt {s['prompt_chars']} chars  system#{s['system_sha']}  "
                  f"tokens in/out={s.get('tokens_in', 0)}/{s.get('tokens_out', 0)}"
                  f"{'~' if s.get('estimated') else ''} think={s.get('thinking', 0)} cached={s.get('cache_read', 0)}")
            print("PROMPT:\n" + clip(s["prompt"]))
            print("REPLY:\n" + clip(s["reply"]))
            if "tool" in s:
                print(f"TOOL {s['tool']['name']} {json.dumps(s['tool']['args'], ensure_ascii=False)}  ({s['tool_s']}s)")
                print("RESULT:\n" + clip(s["tool_result"]))
        print(f"ANSWER:\n{t['answer']}" + (f"\nERROR: {t['error']}" if t["error"] else ""))


if __name__ == "__main__":
    main()
