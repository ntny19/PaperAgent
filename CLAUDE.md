# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PaperAgent: a localhost-only (127.0.0.1:8765) FastAPI + WebSocket chat app. It answers questions over a folder of indexed PDF papers (`PAPERAGENT_PAPER_ROOT` in `.env`) and helps reproduce those papers from code repos ("workspaces"). It uses no API keys: LLMs are reached by shelling out to logged-in CLIs (`agy`, `claude`, `codex`) or a local Ollama.

## Commands

There is no linter or build step.

- Run: `start.bat` (creates `.venv` and installs `requirements.txt` if missing, opens the browser), or manually from the repo root: `.venv\Scripts\python -W ignore -m app.main`. It must run as a module from the repo root, since `app/` uses relative imports.
- Tests: `.venv\Scripts\python -W ignore -m unittest discover -s tests` (offline guard/parser tests). Golden agent evals (real model + index): `.venv\Scripts\python -W ignore -m evals.run_evals [--model ID] [--case ID] [--list] [-v]`; cases live in `evals/golden.yaml`.
- Logs: every turn is appended to `data/logs/YYYY-MM-DD.jsonl` by `app/trace.py` (masked; `PAPERAGENT_LOG=0` disables, `PAPERAGENT_LOG_FULL_PROMPT=1` keeps the system prompt). View with `python -m app.logview [-n N] [--full] [--tag eval]`.
- Model choices and `default_model` in `config.yaml` reload for each state request and chat turn via `app/model_config.py`; the browser refreshes choices every three seconds. Other config and `.env` settings need a restart. `.env` is git-ignored; real environment variables override it.
- `data/index.sqlite` is the search index and is safe to delete (it is rebuilt). Embeddings need Ollama at `embedding.url` with `nomic-embed-text`.

## Architecture

Request flow: `static/index.html` ⇄ `/ws` (`app/main.py`) → `agent.run_turn` → `llm.complete` → parse a tool call → `tools.TOOLS` → result fed back, up to `MAX_STEPS`=10.

- **Text-protocol agent loop (`app/agent.py`)**: models have their native tools disabled, and the prompt is rebuilt from scratch every step (system prompt + full history + this turn's tool activity). The model calls a tool by replying with only a fenced ```` ```tool {"name":..., "args":{...}} ``` ```` block, and anything else is the final answer. `parse_tool_call` is deliberately lenient (accepts ```` ```json ```` or a bare `{"name"...`). The final step forces an answer without tools.
- **Backends (`app/llm.py`)**: each backend is a blocking subprocess/HTTP call run via `asyncio.to_thread`, executed in an empty temp dir (`NEUTRAL`) with the CLI's own tools disabled (`claude --tools ""`, `agy --mode plan`, `codex -s read-only`). Model IDs/backends come from `config.yaml: models`. `agy` retries on empty responses with a "you have no built-in tools" reminder.
- **Tools (`app/tools.py`)**: the `TOOLS` registry maps name → (async fn, doc string); `tools_doc()` injects the docs into the prompt, so adding a tool means adding one registry entry. Tools take a `Ctx` (index, selected workspace, `emit`, `approve`). Raise `ToolError` for user-facing failures; the loop catches everything else. All repo paths go through `_safe` (must stay inside the workspace, `.git` blocked). `run_command` and `edit_file` call `ctx.approve(...)`, which sends an approval card over the WebSocket and awaits the user's click. Stop/disconnect resolve pending approvals as denied.
- **Secret safety (`app/secrets_guard.py`)**: enforced in code, not just prompts. `is_secret_file` blocks reads, `mask_text` redacts everything sent to the model or UI (applied to tool results and the final answer), `check_command` refuses dangerous commands (git commit/push/reset/checkout, env/key printing, `type`/`cat` of secret files), and `scan_repo` finds hardcoded keys and whether git would commit them. When adding any new read or exec path, route it through these helpers.
- **Index (`app/index.py`)**: PyMuPDF text, page-level storage, ~1100-char chunks, SQLite FTS5 (BM25) plus Ollama embeddings (numpy matrix, held in memory) for hybrid search. `main.watcher` calls `Index.scan` every 60 s. Results are cited as `[Paper title p.N]`.
- **Skills (`skills/*.md`)**: not Claude Code skills. They are agent rules concatenated into every system prompt by `agent.load_skills` (alphabetical). `secret-safety.md` is marked mandatory and overrides the others. Edit these to change agent behavior.
- **Workspaces**: `PAPERAGENT_WORKSPACE_<n>` entries in `.env` link each repo path to a paper-name substring. They point at repos outside this project (e.g. `D:/GitHub/...`) and are machine-specific, which is why they are not in `config.yaml`.

The WebSocket handler enforces an Origin allowlist and runs one chat task per connection. `history` lives only in the connection, and the `reset` message clears it.
