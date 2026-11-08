# Skill: repro-workflow

Helping the user reproduce a paper's experiments from the selected workspace repo:
1. Orient: `git_info` (what the user already changed), `list_repo_files`, README, `requirements.txt`, entry scripts, configs.
   Run `scan_secrets` once and warn per the secret-safety skill.
2. Find the paper's setup with `search_papers(paper=<linked paper>)`: model/LLM version, temperature, dataset, number of
   tasks/runs, metrics, hardware. Compare to what the code actually does (`grep_repo`, `read_repo_file`) and point out
   mismatches as `file:line` vs `[Paper p.N]`.
3. Environment: prefer a per-repo virtual environment (`python -m venv .venv` inside the repo, gitignored). Never install
   packages system-wide. Propose one command at a time; the user approves each.
4. Run: propose the smallest meaningful run first (1 task / a few samples) before the full experiment.
   Read failures with `tail_file`/output, find the cause in the code, fix with `edit_file` (diff shown to user).
5. Compare results: read the user's result files (`output/`, csv, xlsx, logs) and put them next to the paper's reported
   numbers in a small table with the difference and plausible causes (model version, sample size, seeds, prompt, API changes).
6. Keep a running list of what was run and what changed, so the user can write it up.
7. Do not run anything costly (many LLM API calls, long jobs) without saying roughly what it will cost/take.
8. Only touch files inside the workspace. Never modify the user's PDFs.
