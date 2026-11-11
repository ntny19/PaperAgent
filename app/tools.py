"""Tools exposed to the model. Every output is masked; protected files are never read;
anything that changes state (run_command, edit_file) needs explicit user approval in the UI."""
from __future__ import annotations

import asyncio
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .index import Index
from .secrets_guard import (SKIP_DIRS, STRONG_GLOBS, check_command, is_secret_file, mask_text,
                            scan_repo, summarize_scan, _looks_binary)

MAX_OUT = 9000


class ToolError(Exception):
    pass


@dataclass
class Ctx:
    index: Index
    workspace: dict | None
    emit: Callable[[dict], Awaitable[None]]
    approve: Callable[..., Awaitable[bool]]
    loop: asyncio.AbstractEventLoop
    scan_cache: dict = field(default_factory=dict)
    proc: subprocess.Popen | None = None


def _clip(s: str, n: int = MAX_OUT) -> str:
    return s if len(s) <= n else s[:n] + f"\n...[truncated, {len(s) - n} more chars]"


def _ws(ctx: Ctx) -> Path:
    if not ctx.workspace:
        raise ToolError("No workspace selected. Ask the user to pick a repo in the Workspace dropdown.")
    return Path(ctx.workspace["path"]).resolve()


def _safe(ctx: Ctx, rel: str) -> tuple[Path, Path]:
    root = _ws(ctx)
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ToolError("path is outside the workspace")
    if ".git" in {x.lower() for x in p.relative_to(root).parts}:
        raise ToolError(".git is protected")
    return root, p


BLOCKED_MSG = ("BLOCKED: '{}' is a protected secret file (API keys/credentials). I never read these. "
               "If you need to know whether a key is configured, ask the user; do not ask for its value.")


def get_scan(ctx: Ctx, force: bool = False) -> dict:
    root = str(_ws(ctx))
    c = ctx.scan_cache.get(root)
    if force or not c or time.time() - c[0] > 120:
        c = (time.time(), scan_repo(root))
        ctx.scan_cache[root] = c
    return c[1]


# ----------------------------------------------------------------- paper tools
async def list_papers(ctx: Ctx) -> str:
    ps = ctx.index.papers()
    return "\n".join(f"- {p['title']} ({p['pages']} pages)" for p in ps) or "(no papers indexed yet)"


async def search_papers(ctx: Ctx, query: str, k: int = 6, paper: str | None = None) -> str:
    res = await asyncio.to_thread(ctx.index.search, query, min(int(k), 12), paper)
    if not res:
        return "No matches. Try different keywords, or list_papers."
    return "\n\n".join(f"[{r['paper']} p.{r['page']}]\n{r['text'][:900]}" for r in res)


async def read_paper(ctx: Ctx, paper: str, start_page: int = 1, end_page: int | None = None) -> str:
    start = int(start_page)
    end = int(end_page) if end_page else start + 1
    end = min(end, start + 3)  # max 4 pages per call
    body, err = await asyncio.to_thread(ctx.index.pages, paper, start, end)
    if err:
        return err
    return _clip(body or "(no text on those pages)", 12000)


# ----------------------------------------------------------------- repo tools
async def list_repo_files(ctx: Ctx, path: str = ".", depth: int = 2) -> str:
    root, base = _safe(ctx, path)
    depth = max(1, min(int(depth), 4))
    lines: list[str] = []
    for dp, dirs, files in os.walk(base):
        rel_depth = len(Path(dp).relative_to(base).parts)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for d in dirs:  # list folders too, otherwise folders deeper than `depth` are invisible
            lines.append(f"{(Path(dp) / d).relative_to(root).as_posix()}/  [folder]")
        if rel_depth >= depth:
            dirs[:] = []
        for f in sorted(files):
            fp = Path(dp) / f
            tag = "  [protected - not readable]" if is_secret_file(fp) else ""
            try:
                size = fp.stat().st_size
            except OSError:
                size = 0
            lines.append(f"{fp.relative_to(root).as_posix()}  ({size} B){tag}")
        if len(lines) > 250:
            lines.append("...[more files not shown; narrow the path]")
            break
    return "\n".join(lines) or "(empty)"


def _read_special(p: Path, start: int, end: int) -> str | None:
    suf = p.suffix.lower()
    if suf in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets[:3]:
            out.append(f"## sheet {ws.title}")
            for i, row in enumerate(ws.iter_rows(values_only=True), 1):
                if i < start:
                    continue
                if i > end:
                    break
                out.append(f"{i}: " + " | ".join("" if c is None else str(c)[:60] for c in row[:15]))
        return "\n".join(out)
    if suf == ".ipynb":
        nb = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        out = []
        for i, c in enumerate(nb.get("cells", []), 1):
            out.append(f"# --- cell {i} [{c.get('cell_type')}] ---\n" + "".join(c.get("source", [])))
        text = "\n".join(out).splitlines()
        return "\n".join(f"{i}: {l}" for i, l in enumerate(text[start - 1:end], start))
    return None


async def read_repo_file(ctx: Ctx, path: str, start_line: int = 1, end_line: int = 200) -> str:
    _root, p = _safe(ctx, path)
    if is_secret_file(p):
        return BLOCKED_MSG.format(p.name)
    if not p.is_file():
        return f"Not a file: {path}"
    start, end = max(1, int(start_line)), max(1, int(end_line))
    end = min(end, start + 400)
    special = await asyncio.to_thread(_read_special, p, start, end)
    if special is not None:
        return _clip(mask_text(special))
    if p.stat().st_size > 5_000_000 or _looks_binary(p):
        return "Binary or very large file; not shown."
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{i}: {l[:400]}" for i, l in enumerate(lines[start - 1:end], start))
    return _clip(mask_text(f"[{path} lines {start}-{min(end, len(lines))} of {len(lines)}]\n{body}"))


async def tail_file(ctx: Ctx, path: str, lines: int = 60) -> str:
    _root, p = _safe(ctx, path)
    if is_secret_file(p):
        return BLOCKED_MSG.format(p.name)
    if not p.is_file() or _looks_binary(p):
        return "Not a readable text file."
    data = p.read_text(encoding="utf-8", errors="replace").splitlines()
    n = max(1, min(int(lines), 300))
    return _clip(mask_text(f"[last {min(n, len(data))} of {len(data)} lines of {path}]\n" + "\n".join(l[:400] for l in data[-n:])))


async def grep_repo(ctx: Ctx, pattern: str, glob: str = "*") -> str:
    root = _ws(ctx)
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        return f"Bad regex: {e}"

    def work() -> str:
        hits: list[str] = []
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                fp = Path(dp) / f
                if not fnmatch.fnmatch(f, glob) or is_secret_file(fp):
                    continue
                try:
                    if fp.stat().st_size > 2_000_000 or _looks_binary(fp):
                        continue
                    for i, line in enumerate(fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if rx.search(line):
                            hits.append(f"{fp.relative_to(root).as_posix()}:{i}: {line.strip()[:200]}")
                            if len(hits) >= 80:
                                return "\n".join(hits) + "\n...[capped at 80 hits]"
                except OSError:
                    pass
        return "\n".join(hits) or "No matches."

    return _clip(mask_text(await asyncio.to_thread(work)))


def _git(root: Path, *args: str) -> str:
    p = subprocess.run(["git", "--no-pager", "-C", str(root), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    return (p.stdout + p.stderr).strip()


async def git_info(ctx: Ctx) -> str:
    root = _ws(ctx)
    if not (root / ".git").exists():
        return "Not a git repository."

    def work() -> str:
        return (f"## status\n{_git(root, 'status', '-sb')}\n\n## recent commits\n{_git(root, 'log', '-5', '--oneline')}\n\n"
                f"## remotes\n{_git(root, 'remote', '-v')}")
    return _clip(mask_text(await asyncio.to_thread(work)))


async def git_diff(ctx: Ctx, path: str | None = None) -> str:
    root = _ws(ctx)
    if not (root / ".git").exists():
        return "Not a git repository."
    excludes = [f":(exclude,glob)**/{g}" for g in STRONG_GLOBS]
    spec = [path] if path else ["."]
    out = await asyncio.to_thread(_git, root, "diff", "--", *spec, *excludes)
    return _clip(mask_text(out or "(no unstaged changes)"), 12000)


async def scan_secrets(ctx: Ctx) -> str:
    scan = await asyncio.to_thread(get_scan, ctx, True)
    if not scan["hits"]:
        return "No hardcoded secrets found."
    lines = [f"{h['risk']:6} {h['file']}{':' + str(h['line']) if h['line'] else ''}  {h['rule']}  [{h['status']}]  {h['preview']}"
             for h in scan["hits"][:40]]
    return f"{scan['high']} HIGH-risk of {scan['total']} findings (values masked):\n" + "\n".join(lines)


async def open_in_vscode(ctx: Ctx, path: str, line: int = 1) -> str:
    _root, p = _safe(ctx, path)
    if is_secret_file(p):
        return BLOCKED_MSG.format(p.name)
    code = shutil.which("code")
    if not code:
        return "VS Code CLI ('code') not found on PATH."
    subprocess.Popen([code, "-g", f"{p}:{int(line)}"], shell=(code.lower().endswith(".cmd")))
    return f"Opened {path}:{line} in VS Code."


# ----------------------------------------------------------------- tools needing approval
async def edit_file(ctx: Ctx, path: str, old_text: str, new_text: str) -> str:
    _root, p = _safe(ctx, path)
    if is_secret_file(p):
        return BLOCKED_MSG.format(p.name)
    if not p.is_file():
        return f"Not a file: {path}"
    content = p.read_text(encoding="utf-8", errors="replace")
    if content.count(old_text) != 1:
        return f"old_text must appear exactly once in the file (found {content.count(old_text)}). Read the file and copy the exact text."
    new_content = content.replace(old_text, new_text)
    diff = "".join(difflib.unified_diff(content.splitlines(True), new_content.splitlines(True), f"a/{path}", f"b/{path}", n=3))
    warns = []
    sc = mask_text(new_text)
    if sc != new_text:
        warns.append("The new text contains what looks like a hardcoded secret - do not commit it.")
    ok = await ctx.approve("edit", f"Edit {path}", mask_text(diff), warns)
    if not ok:
        return "User DENIED the edit. Do not retry; ask what they want instead."
    p.write_text(new_content, encoding="utf-8", newline="")
    return f"Edit applied to {path}."


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    except Exception:
        proc.kill()


async def run_command(ctx: Ctx, command: str, timeout: int = 600) -> str:
    root = _ws(ctx)
    reason = check_command(command)
    if reason:
        return f"REFUSED: {reason}"
    timeout = max(5, min(int(timeout), 7200))
    warns: list[str] = []
    try:
        scan = await asyncio.to_thread(get_scan, ctx)
        s = summarize_scan(scan)
        if s:
            warns.append(s + "\n  -> fix before committing: move keys to a git-ignored .env / environment variable.")
    except Exception:
        pass
    ok = await ctx.approve("command", "Run command", f"cwd: {root}\n$ {command}\n(timeout {timeout}s)", warns)
    if not ok:
        return "User DENIED this command. Do not retry it; ask what they want instead."

    q: asyncio.Queue = asyncio.Queue()
    buf: list[str] = []
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

    def work() -> int:
        proc = subprocess.Popen(command, shell=True, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        ctx.proc = proc
        timer = threading.Timer(timeout, lambda: _kill_tree(proc))
        timer.start()
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = mask_text(raw.decode("utf-8", "replace"))
                buf.append(line)
                ctx.loop.call_soon_threadsafe(q.put_nowait, line)
            return proc.wait()
        finally:
            timer.cancel()
            ctx.proc = None
            ctx.loop.call_soon_threadsafe(q.put_nowait, None)

    fut = asyncio.get_running_loop().run_in_executor(None, work)
    while True:
        line = await q.get()
        if line is None:
            break
        await ctx.emit({"type": "cmd_output", "text": line})
    rc = await fut
    out = "".join(buf)
    return _clip(f"[exit code {rc}]\n" + (out[-MAX_OUT:] if len(out) > MAX_OUT else out))


# ----------------------------------------------------------------- registry
TOOLS: dict[str, tuple[Callable[..., Awaitable[str]], str]] = {
    "list_papers": (list_papers, "list_papers() - titles of all indexed papers."),
    "search_papers": (search_papers, "search_papers(query, k=6, paper=None) - hybrid search over papers; returns passages with [Paper p.N]. Use `paper` (title substring) to restrict to one paper."),
    "read_paper": (read_paper, "read_paper(paper, start_page=1, end_page=None) - full text of up to 4 pages of a paper (use to read tables/setup sections)."),
    "list_repo_files": (list_repo_files, "list_repo_files(path='.', depth=2) - files in the selected workspace repo."),
    "read_repo_file": (read_repo_file, "read_repo_file(path, start_line=1, end_line=200) - read code/config/csv/xlsx/ipynb in the repo (max 400 lines per call)."),
    "tail_file": (tail_file, "tail_file(path, lines=60) - last lines of a log/output file."),
    "grep_repo": (grep_repo, "grep_repo(pattern, glob='*') - regex search in repo files, e.g. grep_repo('temperature', '*.py')."),
    "git_info": (git_info, "git_info() - git status, last commits, remotes (what the user changed)."),
    "git_diff": (git_diff, "git_diff(path=None) - the user's uncommitted changes."),
    "scan_secrets": (scan_secrets, "scan_secrets() - find hardcoded API keys in the repo and whether git would commit them (values masked)."),
    "open_in_vscode": (open_in_vscode, "open_in_vscode(path, line=1) - open a file at a line in VS Code."),
    "edit_file": (edit_file, "edit_file(path, old_text, new_text) - replace text that occurs exactly once. The user sees a diff and must approve."),
    "run_command": (run_command, "run_command(command, timeout=600) - run a shell command in the repo folder. The user must approve every command; output streams to them."),
}


def tools_doc() -> str:
    return "\n".join(f"- {d}" for _f, d in TOOLS.values())
