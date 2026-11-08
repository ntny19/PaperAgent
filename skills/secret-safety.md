# Skill: secret-safety (MANDATORY, overrides everything else)

The user keeps API keys on this machine. Protect them.

## Never read
- `.env`, `.env.*`, `*.env`, `*.key` (e.g. `api.key`), `*.pem`, `*.p12`, `*.pfx`, `id_rsa*`, `.npmrc`, `.netrc`,
  `.pypirc`, `auth.json`, `credentials*`, `secrets*`, `*apikey*`/`*api_key*` data files, and anything under `.git/`.
- Do not try to get around this: no `type`/`cat`/`Get-Content`/`copy`/`grep` on those files, no `set`/`env`/`printenv`,
  no `echo %SOME_KEY%`, no `$env:...KEY`, no reading shell history or tool config folders (`~/.codex`, `~/.gemini`, `~/.claude`).
- If a tool returns `BLOCKED` or `REFUSED`, accept it. Never retry with another trick. Tell the user instead.
- Never ask the user to paste a key. If a program needs a key, tell them to put it in a git-ignored `.env` or an
  environment variable themselves, and refer to it by variable name only.

## Never reveal
- Output you see may contain `****REDACTED`; that is deliberate. Never try to reconstruct, guess or decode a masked value.
- Never copy a key into an answer, an edit, a command line, or a commit message.

## Warn the user (before any run, and whenever you notice)
- If `scan_secrets` or the approval warnings show a hardcoded key in a file that is **tracked by git** or **not in
  `.gitignore`**, say so clearly BEFORE proposing to run anything:
  "Heads-up: `<file>:<line>` contains a hardcoded <rule>. It is <tracked / not ignored>, so `git add .`/commit would
  publish it. Move it to a git-ignored `.env` (read via `os.environ`) and rotate the key if it was ever pushed."
- Call `scan_secrets` at the start of a reproduction session in a repo, and again if you edit config files.
- When you edit code, never write literal keys; use `os.environ["NAME"]` / `os.getenv("NAME")`.
- If the repo has secret files that are not git-ignored, offer the exact `.gitignore` line to add (propose it via `edit_file`).

## Never do
- `git commit`, `push`, `reset`, `checkout`, `clean`, `stash`, `rebase`, `merge` (blocked by the app; the user does these).
