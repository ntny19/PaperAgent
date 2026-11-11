from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import agent
from .index import Index
from .model_config import load_models
from .secrets_guard import scan_repo
from .tools import Ctx, _kill_tree

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Tiny .env reader (no dependency). Real environment variables win over the file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def env_workspaces() -> list[dict]:
    """PAPERAGENT_WORKSPACE_<n>=Name|path|paper-name-substring  (paper part optional)."""
    items = []
    for k, v in os.environ.items():
        m = re.fullmatch(r"PAPERAGENT_WORKSPACE_(\d+)", k)
        if m and v.strip():
            parts = [p.strip() for p in v.split("|")]
            if len(parts) < 2:
                print(f"[config] ignoring {k}: expected 'Name|path|paper'")
                continue
            items.append((int(m.group(1)), {"name": parts[0], "path": parts[1], "paper": parts[2] if len(parts) > 2 else parts[0]}))
    return [w for _n, w in sorted(items, key=lambda t: t[0])]


load_dotenv(ROOT / ".env")
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
if not os.environ.get("PAPERAGENT_PAPER_ROOT"):
    sys.exit("PAPERAGENT_PAPER_ROOT is not set. Copy .env.example to .env and set it to your papers folder.")
CFG["paper_root"] = os.environ["PAPERAGENT_PAPER_ROOT"]
CFG["data_dir"] = os.environ.get("PAPERAGENT_DATA_DIR") or str(ROOT / "data")
CFG["port"] = int(os.environ.get("PAPERAGENT_PORT") or CFG.get("port", 8765))
CFG["embedding"]["url"] = os.environ.get("PAPERAGENT_OLLAMA_URL") or CFG["embedding"]["url"]
CFG["embedding"]["model"] = os.environ.get("PAPERAGENT_EMBED_MODEL") or CFG["embedding"]["model"]
CFG["workspaces"] = env_workspaces()
load_models(ROOT / "config.yaml")  # Validate the catalog at startup.
WORKSPACES = {w["name"]: w for w in CFG["workspaces"]}
INDEX = Index(CFG["paper_root"], CFG["data_dir"], CFG["embedding"]["url"], CFG["embedding"]["model"])
OLLAMA = CFG["embedding"]["url"]
ALLOWED_ORIGINS = {f"http://127.0.0.1:{CFG['port']}", f"http://localhost:{CFG['port']}"}


async def watcher():
    """Pick up new/changed PDFs automatically."""
    while True:
        await asyncio.to_thread(INDEX.scan)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(watcher())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/")
def home():
    return FileResponse(ROOT / "app" / "static" / "index.html")


@app.get("/api/state")
def state():
    models, default_model = load_models(ROOT / "config.yaml")
    return {
        "models": [{"id": m["id"], "label": m["label"]} for m in models.values()],
        "default_model": default_model,
        "workspaces": [{"name": w["name"], "path": w["path"], "exists": Path(w["path"]).exists()} for w in CFG.get("workspaces", [])],
        "index": INDEX.status,
        "papers": len(INDEX.papers()),
    }


@app.post("/api/reindex")
async def reindex():
    asyncio.create_task(asyncio.to_thread(INDEX.scan))
    return {"ok": True}


@app.get("/api/scan")
async def scan(workspace: str):
    w = WORKSPACES.get(workspace)
    if not w or not Path(w["path"]).exists():
        return JSONResponse({"error": "unknown workspace"}, status_code=404)
    return await asyncio.to_thread(scan_repo, w["path"])


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    pending: dict[str, asyncio.Future] = {}
    history: list[dict] = []
    state_: dict = {"task": None, "ctx": None}
    send_lock = asyncio.Lock()

    async def emit(ev: dict):
        async with send_lock:
            await ws.send_json(ev)

    async def approve(kind: str, title: str, body: str, warnings: list[str]) -> bool:
        aid = uuid.uuid4().hex[:8]
        fut = loop.create_future()
        pending[aid] = fut
        await emit({"type": "approval", "id": aid, "kind": kind, "title": title, "body": body, "warnings": warnings})
        return await fut

    async def do_chat(msg: dict):
        try:
            models, default_model = load_models(ROOT / "config.yaml")
            selected = msg.get("model") or default_model
            if selected not in models:
                raise ValueError(f"Unknown model {selected!r}; refresh the model picker and select again")
            model = models[selected]
        except (ValueError, OSError, yaml.YAMLError) as e:
            await emit({"type": "error", "text": str(e)})
            state_["task"] = None
            return
        w = WORKSPACES.get(msg.get("workspace") or "")
        ctx = Ctx(index=INDEX, workspace=w, emit=emit, approve=approve, loop=loop)
        state_["ctx"] = ctx
        history.append({"role": "user", "content": msg["text"]})
        try:
            answer = await agent.run_turn(ctx, history, model, OLLAMA, tag="chat")
            history.append({"role": "assistant", "content": answer})
            await emit({"type": "final", "text": answer})
        except asyncio.CancelledError:
            await emit({"type": "final", "text": "_(stopped)_"})
        except Exception as e:
            await emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            state_["task"] = None

    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")
            if t == "chat" and not state_["task"]:
                state_["task"] = asyncio.create_task(do_chat(msg))
            elif t == "approval":
                fut = pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(bool(msg.get("approved")))
            elif t == "stop":
                ctx = state_["ctx"]
                if ctx and ctx.proc:
                    _kill_tree(ctx.proc)
                for f in pending.values():
                    if not f.done():
                        f.set_result(False)
                if state_["task"]:
                    state_["task"].cancel()
            elif t == "reset":
                history.clear()
    except WebSocketDisconnect:
        pass
    finally:
        for f in pending.values():
            if not f.done():
                f.set_result(False)
        if state_["task"]:
            state_["task"].cancel()


def main():
    uvicorn.run(app, host="127.0.0.1", port=CFG["port"], log_level="warning")


if __name__ == "__main__":
    main()
