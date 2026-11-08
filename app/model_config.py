"""Reload model choices independently of machine and index settings."""
from pathlib import Path

import yaml


def load_models(path: Path) -> tuple[dict[str, dict], str]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = cfg.get("models") if isinstance(cfg, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("config.yaml: models must be a non-empty list")
    models = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("config.yaml: each model must be a mapping")
        for field in ("id", "label", "backend"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"config.yaml: model {field} must be a non-empty string")
        if entry["id"] in models:
            raise ValueError(f"config.yaml: duplicate model id {entry['id']}")
        if entry["backend"] not in {"agy", "codex", "claude", "ollama"}:
            raise ValueError(f"config.yaml: unknown backend {entry['backend']}")
        if not isinstance(entry.get("model", ""), str):
            raise ValueError("config.yaml: model name must be a string")
        models[entry["id"]] = entry
    default = cfg.get("default_model")
    if not isinstance(default, str) or default not in models:
        raise ValueError("config.yaml: default_model must name a configured model id")
    return models, default
