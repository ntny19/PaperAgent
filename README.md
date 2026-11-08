# PaperAgent

Local chat agent over a folder of papers you have read (PDFs), plus the code repos you reproduce them with.
Ask where a detail is, get answers with page citations, and get help running and checking a paper's code.
No API keys: it uses your logged-in `agy` (Antigravity), `claude` (Claude Code) and `codex` CLIs, or a local Ollama model.

## Setup (Windows)
Requirements: Python 3.11+, [Ollama](https://ollama.com) with `ollama pull nomic-embed-text` (search embeddings),
and at least one logged-in model CLI (`agy`, `claude`, `codex`) or an Ollama chat model.

1. Clone this repo.
2. Run `start.bat`. On first run it creates `.env` from `.env.example` and opens it: set `PAPERAGENT_PAPER_ROOT`
   to your papers folder and add your repos as `PAPERAGENT_WORKSPACE_1=Name|path|paper name`. Save, run `start.bat` again.
3. It creates a `.venv`, installs packages, and opens http://127.0.0.1:8765. The first indexing of ~30 papers takes
   about 25 minutes on CPU (progress shows in the top bar); new PDFs later are picked up within a minute.

Settings: `.env` = your machine (paths, port; git-ignored) · `config.yaml` = shared defaults (model list; edit to
remove models you don't have). Model choices and `default_model` reload automatically within three seconds;
restart the server for other settings. A running reply keeps its selected model; the next message uses the picker.

OpenAI models use the `codex` backend with your logged-in Codex CLI. Choose **Codex (default model)** to
inherit the CLI's configured model, or a named GPT entry to pass that exact model with `codex exec -m`.
The default for new browsers is Codex; an existing browser keeps its saved selection.
You can add any model supported by your CLI/account to `config.yaml`, for example:

```yaml
  - {id: my-openai-model, label: "My OpenAI model", backend: codex, model: "exact-model-id"}
```

Use an ID offered by `/model` in your logged-in Codex CLI. Listed choices do not guarantee account access;
CLI authentication and model-access errors appear in the chat. This project setting controls PaperAgent's
subprocesses; it does not change the model running the Codex session editing this project.

## Using it
- **Model** dropdown: switch model per message. **Workspace** dropdown: pick a repo so the agent can read its code, logs,
  results and git changes. **New chat** clears the conversation (history is not saved yet).
- Every command and file edit waits for your **Approve** click. **Stop** kills a running command.

## Safety (enforced in code, not only in prompts)
- Never reads `.env*`, `*.key`, `*.pem`, `id_rsa*`, `.npmrc`, `credentials*`, `.git/` ... (`app/secrets_guard.py`).
- Everything sent to a model is masked (`sk-a****REDACTED`).
- Blocked commands: git commit/push/reset/checkout/..., printing env vars/keys, `type`/`cat` on secret files.
- When you pick a workspace and before each run, the repo is scanned for hardcoded keys and you are warned if git
  would commit them.
- Agent rules ("skills") are in `skills/*.md` and are added to every prompt.

## Layout
`app/` server, agent loop, tools, index · `skills/` agent rules · `data/` search index (git-ignored, safe to delete) ·
`config.yaml` shared defaults · `.env.example` local settings template

Windows only for now (`start.bat`, `taskkill`, `cmd` shell).
