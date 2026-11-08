"""Secret protection: file blocklist, text masking, and hardcoded-key scanner.

Rules enforced in code (not just in prompts):
  * protected files (.env, *.key, *.pem ...) are never read by any tool;
  * every string returned to a model is passed through mask_text();
  * scan_repo() finds hardcoded keys and says whether git would commit them.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

STRONG_GLOBS = [
    ".env", ".env.*", "*.env", "*.key", "*.pem", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", ".npmrc", ".netrc", ".pypirc", "auth.json",
    "service-account*.json", "*.token", ".git-credentials",
]
FUZZY_GLOBS = ["*apikey*", "*api_key*", "*api-key*", "credentials*", "secrets*", "secret.*", "token*"]
FUZZY_SUFFIXES = {"", ".txt", ".json", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml", ".properties", ".key"}
TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "site-packages", "dist", "build", ".mypy_cache"}

PLACEHOLDER = re.compile(
    r"(?i)(your|xxx|example|placeholder|changeme|dummy|<.*>|\$\{|%\(|\{\{|redacted|\*{3,}|^none$|^null$|^true$|^false$)"
)

# (rule name, regex, is_assignment_rule)
PATTERNS = [
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), False),
    ("OpenAI key", re.compile(r"sk-(?:proj-|live-|svcacct-)?[A-Za-z0-9_\-]{20,}"), False),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}"), False),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,}"), False),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}"), False),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), False),
    ("HuggingFace token", re.compile(r"hf_[A-Za-z0-9]{30,}"), False),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), False),
    ("Credential in URL", re.compile(r"https?://[^/\s:@]+:([^@\s/]{6,})@"), True),
    (
        "Hardcoded credential",
        re.compile(
            r"""(?i)\b[\w.\-]*(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)[\w.\-]*\s*[:=]\s*['"]([^'"\s]{12,})['"]"""
        ),
        True,
    ),
    (
        "Hardcoded credential (config file)",
        re.compile(
            r"""(?im)^\s*[\w.\-]*(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)[\w.\-]*\s*[:=]\s*([^\s'"#]{12,})\s*$"""
        ),
        True,
    ),
]
CONFIG_EXTS = {".ini", ".cfg", ".conf", ".yaml", ".yml", ".toml", ".properties", ".txt", ".json", ".env"}


def is_secret_file(path: str | os.PathLike) -> bool:
    p = Path(path)
    name = p.name.lower()
    if name.endswith(TEMPLATE_SUFFIXES):
        return False
    if any(fnmatch.fnmatch(name, g) for g in STRONG_GLOBS):
        return True
    if p.suffix.lower() in FUZZY_SUFFIXES and any(fnmatch.fnmatch(name, g) for g in FUZZY_GLOBS):
        return True
    return False


def _mask_val(v: str) -> str:
    return (v[:4] if len(v) > 14 else "") + "****REDACTED"


def mask_text(text: str) -> str:
    if not text:
        return text

    def make_sub(is_assign: bool):
        def sub(m: re.Match) -> str:
            if m.lastindex:
                val = m.group(m.lastindex)
                if is_assign and PLACEHOLDER.search(val):
                    return m.group(0)
                s, e = m.span(m.lastindex)
                off = m.start()
                full = m.group(0)
                return full[: s - off] + _mask_val(val) + full[e - off:]
            return _mask_val(m.group(0))
        return sub

    for _rule, rx, is_assign in PATTERNS:
        text = rx.sub(make_sub(is_assign), text)
    return text


def _git(repo: Path, *args: str, stdin: str | None = None, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args], input=stdin, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.returncode, p.stdout
    except Exception:
        return 1, ""


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(2048)
    except OSError:
        return True


def scan_repo(repo_path: str, max_files: int = 8000) -> dict:
    """Find hardcoded secrets. Never returns secret values (only masked previews)."""
    repo = Path(repo_path)
    hits: list[dict] = []
    seen = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            seen += 1
            if seen > max_files:
                break
            fp = Path(root) / fn
            rel = fp.relative_to(repo).as_posix()
            if "dummy" in rel.lower():  # test fixtures (e.g. droidbot dummy documents), not real credentials
                continue
            if is_secret_file(fp):
                hits.append({"file": rel, "line": 0, "rule": "Protected secret file present", "preview": "(content not read)"})
                continue
            try:
                if fp.stat().st_size > 2_000_000 or _looks_binary(fp):
                    continue
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            is_cfg = fp.suffix.lower() in CONFIG_EXTS
            for i, line in enumerate(text.splitlines(), 1):
                if len(line) > 5000:
                    continue
                for rule, rx, is_assign in PATTERNS:
                    if rule.endswith("(config file)") and not is_cfg:
                        continue
                    m = rx.search(line)
                    if not m:
                        continue
                    if is_assign and m.lastindex and PLACEHOLDER.search(m.group(m.lastindex)):
                        continue
                    hits.append({"file": rel, "line": i, "rule": rule, "preview": mask_text(line.strip())[:120]})
                    break
    # git status of each affected file
    files_hit = sorted({h["file"] for h in hits})
    is_git = (repo / ".git").exists()
    tracked: set[str] = set()
    ignored: set[str] = set()
    if is_git and files_hit:
        rc, out = _git(repo, "ls-files", "-z")
        tracked = {x for x in out.split("\0") if x}
        rc, out = _git(repo, "check-ignore", "--stdin", "-z", stdin="\0".join(files_hit) + "\0")
        ignored = {x for x in out.split("\0") if x}
    for h in hits:
        f = h["file"]
        if not is_git:
            h["risk"], h["status"] = "MEDIUM", "not a git repo (cannot tell if it would be committed)"
        elif f in tracked:
            h["risk"], h["status"] = "HIGH", "TRACKED by git - already committed/staged"
        elif f in ignored:
            h["risk"], h["status"] = "OK", "git-ignored"
        else:
            h["risk"], h["status"] = "HIGH", "NOT in .gitignore - `git add .` would commit it"
    hits.sort(key=lambda h: ({"HIGH": 0, "MEDIUM": 1, "OK": 2}[h["risk"]], h["file"], h["line"]))
    return {
        "repo": str(repo),
        "total": len(hits),
        "high": sum(1 for h in hits if h["risk"] == "HIGH"),
        "hits": hits[:200],
    }


def summarize_scan(scan: dict, limit: int = 8) -> str:
    risky = [h for h in scan["hits"] if h["risk"] in ("HIGH", "MEDIUM")]
    if not risky:
        return ""
    lines = [f"{h['file']}{':' + str(h['line']) if h['line'] else ''} - {h['rule']} - {h['status']}" for h in risky[:limit]]
    more = f" (+{len(risky) - limit} more)" if len(risky) > limit else ""
    return "Hardcoded secrets that could be committed:\n  " + "\n  ".join(lines) + more


# --- command policy -------------------------------------------------------
GIT_BLOCKED = re.compile(
    r"\bgit\s+(?:-\S+\s+)*(reset|checkout|switch|clean|commit|push|rebase|merge|stash|restore|rm|cherry-pick|revert|tag|am|apply|pull|fetch|branch\s+-[dD])\b",
    re.I,
)
ENV_DUMP = re.compile(
    r"(?i)(^\s*(set|env|printenv)\s*$|%[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*%|\$env:[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)|Get-ChildItem\s+env:|\bgci\s+env:|\$\{?[A-Z0-9_]*(API_KEY|TOKEN|SECRET)[A-Z0-9_]*\}?)"
)
READERS = re.compile(r"(?i)\b(type|cat|more|less|head|tail|get-content|gc|copy|cp|xcopy|findstr|grep|select-string|sls|curl|scp|rg)\b")


def check_command(command: str) -> str | None:
    """Return a reason if the command must be refused, else None."""
    if GIT_BLOCKED.search(command):
        return "git write operations (reset/checkout/commit/push/...) are blocked - run these yourself."
    if ENV_DUMP.search(command):
        return "commands that print environment variables / API keys are blocked."
    for tok in re.findall(r"""[^\s"'<>|&;]+""", command):
        if is_secret_file(tok.replace("\\", "/").split("/")[-1]) and READERS.search(command):
            return f"reading a protected secret file ({tok}) is blocked."
        if is_secret_file(tok.replace("\\", "/").split("/")[-1]) and re.search(r"[<>]", command):
            return f"redirecting a protected secret file ({tok}) is blocked."
    return None
