# Relay Agent — repeatable public release checklist

Checked against the public repository on **2026-07-25**. Re-run this gate after changes to collectors, integrations or publishing paths.

## 1. Secrets and credentials

- [x] No `.env` file is tracked.
- [x] No API token, bot token, cookie, browser profile or private key is present.
- [x] `.env.example` contains placeholders only.
- [x] Git history has been checked, not only the current working tree.

## 2. Private source material

- [x] Raw Codex sessions are excluded.
- [x] Terminal history is excluded.
- [x] Private repository names and paths are removed.
- [x] Client, employer and personal information is removed.
- [x] Screenshots do not expose usernames, tokens, email addresses or local paths.

## 3. Safe examples

- [x] Examples are synthetic.
- [x] Fixtures contain no copied private prompts or outputs.
- [x] Sample summaries are clearly marked as examples.
- [x] The repository explains what data stays local.

## 4. Architecture and boundaries

- [x] Input sources are documented.
- [x] Sanitization rules are documented and tested.
- [x] Output package format is documented.
- [x] Duplicate prevention is documented.
- [x] Failure and retry behavior is documented.
- [x] Publication is separate, explicit and opt-in.

## 5. Repository hygiene

- [x] `.gitignore` covers logs, caches, databases and generated inbox files.
- [x] Installation instructions work without private services.
- [x] Tests run without private services (26 passing).
- [x] The tracked tree and repository history were scanned for common secret patterns.
- [x] The social preview and README contain no sensitive material.
