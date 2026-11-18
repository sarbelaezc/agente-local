# Agent instructions

These are the project rules for every coding assistant. GitHub Copilot and other tools that
follow the AGENTS.md convention load this file directly. Claude Code does not read
`AGENTS.md` on its own, so `CLAUDE.md` contains only an import of this file. Keep every rule
here: do not add rules to `CLAUDE.md` or create `.github/copilot-instructions.md`.

## Project

This project is a local agent that runs on demand on the user's own machine, with local Ollama models and no external AI services. Its features are the Hotmail invoice organizer and the Outlook monthly expense close.

The priorities are:

1. Security.
2. Human control.
3. Reproducible automation.
4. Privacy.
5. Auditability.

The project is a standalone repository; personal paths are defined in local configuration files excluded from Git.

## Evidence and clarification

- Do not assume missing facts, requirements, user intent, or the cause of a failure.
- Verify technical facts with the repository, tests, or direct runtime evidence before changing behavior or claiming success.
- If anything remains unclear or requires a user decision, ask the user first and wait for an answer before proceeding with the affected action.
- Never replace an unanswered question with an implicit default or an unverified assumption.

## Feature management and README

The local agent groups several features (for example, Hotmail invoices and the monthly expense close). The user's requests define what gets implemented; the `README.md` documents what has been requested and implemented, but it is not the source of truth and does not limit what the user can request.

### Start of each work session

- Before performing any task, ask the user which feature they want to implement or modify.
- Offer as options the features listed in the README feature table, with their status, plus the option to create a new feature.
- Do not read code in detail, edit files, install dependencies, or run automations until the user answers.
- Limit the work to the chosen feature. If a task requires touching another feature, warn the user and ask for confirmation first.

### Declaring in the README

- Every new feature or change requested by the user must be declared in the `README.md` in the same change in which it is implemented.
- Each feature must appear in the feature table with its name, status, module, and run command, and have its own section covering scope, configuration, usage, and security restrictions.
- If a change alters a feature's behavior, commands, configuration, dependencies, or status, update the `README.md` to reflect it.
- In the `README.md`, distinguish what is implemented and verified from what is pending or not validated.
- If the `README.md` describes something different from what the user is requesting now, the user's request prevails: implement it and update the `README.md`. If the request contradicts a security rule in these instructions, report the conflict before continuing.

## Preferred technologies

- Python 3.12.
- Playwright for Python, only for the dashboard tests. No feature drives a browser.
- Ollama for the local model.
- Microsoft Graph with MSAL for the monthly close Outlook mailbox, using only the read-only Mail.Read permission.
- Microsoft Graph with MSAL for the invoice mailbox, through the same Azure app registration as the monthly close with Mail.ReadWrite added, restricted in code to reading messages and moving approved ones.
- The Gmail API with `google-auth-oauthlib` (sign-in and token refresh only) for a monthly close Gmail mailbox, using only the read-only `gmail.readonly` scope.
- Tesseract (local) and `pypdfium2` for scanned PDF OCR.
- SQLite for local state, configuration, and auditing.
- A local web panel on 127.0.0.1 for review and approval (exact Host and Origin, per-run CSRF token, no external resources), in the style of the monthly close dashboard. No Tkinter.
- Pytest for tests.

Do not add unnecessary dependencies without a clear reason.

## Architecture

```text
src/
  shared/             helpers every feature uses: storage.py (local data folder),
                      local_model.py (Ollama client), run_log.py (per-run logs)
  mail_invoices/      Hotmail invoices
  monthly_close/      monthly close through Microsoft Graph (Outlook) and the Gmail API
tests/
  conftest.py         points every test at a temporary data folder
  shared/
  mail_invoices/
  monthly_close/
scripts/
  agente-local.ps1    single launcher for both features
config/               templates only; personal files are ignored by Git
.local/               all local data and configuration, ignored by Git
```

Code used by more than one feature lives in `src/shared/`, never inside one feature's
package; it must not depend on any feature. Each feature keeps its own data, state and
Ollama usage; keep these separated within a feature:

- Mailbox access (Microsoft Graph for invoices; Microsoft Graph and the Gmail API for the monthly close).
- Ollama integration.
- Approval interface.
- Persistence and auditing.
- Configuration.

## Mandatory rules

- Never perform a destructive or irreversible action (write to a workbook, move a message) without explicit human approval.
- Do not store passwords, tokens, or cookies in source code.
- Do not ask the user for credentials through the agent.
- Request only the mailbox API permissions a feature needs (monthly close: Microsoft Graph `Mail.Read` and Gmail `gmail.readonly`; invoices: Microsoft Graph `Mail.ReadWrite`). Both features use one Azure app, by the user's decision, so its tokens may carry `Mail.ReadWrite` for either feature: each client must therefore enforce its own allow-list of operations in code (monthly close: GET only; invoices: reads and the move action only; never delete, send, flag or mark as read), and each feature keeps its own token cache. Keep token caches and OAuth client files out of Git (they live in `.local/`). Send Graph tokens only to graph.microsoft.com and Gmail tokens only to gmail.googleapis.com, apart from the provider's own sign-in and token-refresh endpoints.
- Stop on MFA, captcha, unexpected authentication, or unrecognized screens.
- Do not allow the model direct access to the terminal, disk, or network.
- Do not execute code generated by the model.
- Do not store unnecessary message content.
- Stop all activity on a failure or an ambiguous selector.
- Exception for monthly close: after the single local correction fails literal-evidence validation (including a correction that changes other fields or drops movements), persist the notice metadata as a visible extraction-review item and continue scanning. Never import the invalid proposal or mark that notice processed; keep coverage partial and retry on the next scan. A notice that states money left the account but yields no movement is queued the same way, never dropped. Session, Microsoft Graph and other validation failures still stop all activity.
- Record who approved a change and when.

## Browser automation rules

No feature drives a browser today: invoices moved to Microsoft Graph. These rules apply if a future feature does.

- Prefer Playwright over a visual agent.
- Use a persistent browser profile separate from the user's usual one.
- Use event- or element-based waits rather than fixed delays.
- Write selectors in a stable, centralized way.
- Detect web application version changes and fail without acting.

## Local model integration

- Communicate with Ollama through an isolated client.
- Configure the model and its parameters from local configuration.
- Do not use external tools inside Ollama.
- Build context exclusively from data explicitly supplied by the user or read from authorized sources.
- Tell the model not to invent information.
- Treat all model output as untrusted text; validate it against the source before using it.
- When a notice has a fixed wording (for example a card payment), prefer reading it with a deterministic parser over asking the model.

## Privacy and auditing

- Minimize retained data.
- Keep only what auditing requires.
- Avoid logs with full content unless necessary.
- Allow manual deletion of temporary records.
- Never commit personal configuration, workbooks, invoices, browser profiles or databases.

## Local data

- All local data and configuration lives in `.local/` inside the project (`.local/MonthlyCloseAgent`, `.local/MailInvoiceAgent`) and in `config/settings.yaml`. Git ignores them; never stage, commit, or copy them into tracked files.
- `shared.storage.data_root()` resolves that folder; `AGENTE_LOCAL_DATA` overrides it. Do not hardcode other locations.
- `.local/` holds real financial records and Microsoft and Google session tokens. Read it only when the task requires it, never print message bodies, tokens or cookies, and back up a database before changing it outside the normal commands.
- Tests must never read or write `.local/`: `tests/conftest.py` redirects them to a temporary folder.

## Commands and structure

- Keep configuration files out of the source code.
- Provide clear scripts for setup, execution, and testing.
- Keep the README Windows installation instructions up to date.
- Setup: `.\scripts\bootstrap.ps1`. Tests: `.\.venv\Scripts\python.exe -m pytest` (a feature: `... -m pytest tests\monthly_close`).
- Run: `.\scripts\agente-local.ps1 <command>`; the README table lists the commands and which ones use Ollama.
- The shell is Windows PowerShell 5.1: no `&&`, and `$PSScriptRoot` is empty while parameter defaults are evaluated under `-File`.

## Assistant tooling

### Project skills

The shared project skills live only in `.claude/skills/`, which both GitHub
Copilot and Claude Code read. Choose and load the single best-matching skill
before starting a task when one applies; do not load unrelated skills. If a task
has distinct needs that match multiple skills, use one primary skill and only
the additional skill needed for the separate need. Do not force a skill when
none fits.

| Skill | Use for |
|---|---|
| [agent-browser](.claude/skills/agent-browser/SKILL.md) | Browser or Electron automation, site testing, and browser-based QA. Follow the browser automation rules; no feature automates a mailbox through a browser. |
| [documentation-writer](.claude/skills/documentation-writer/SKILL.md) | Writing or restructuring the README and `docs/` with the Diátaxis types (tutorial, how-to, reference, explanation). Its "do not copy" rule does not apply when moving this project's own documentation; propose the outline before rewriting. |
| [find-skills](.claude/skills/find-skills/SKILL.md) | Finding skills when the user asks to discover or extend agent capabilities. Do not install or replace skills without the user's request; keep project skills in the canonical directory. |
| [frontend-design](.claude/skills/frontend-design/SKILL.md) | Building or substantially redesigning a user interface. |
| [pdf](.claude/skills/pdf/SKILL.md) | Explicitly requested PDF reading, extraction, creation, conversion, or form work. Follow approval, privacy, and no-generated-code rules; a skill does not authorize file writes or external sharing. |
| [ponytail](.claude/skills/ponytail/SKILL.md) | Coding tasks where the smallest complete change is preferred, without cutting security, validation, or tests. |
| [pytest-coverage](.claude/skills/pytest-coverage/SKILL.md) | Measuring test coverage of a change with `pytest-cov` (dev extra) and adding tests for uncovered lines. Write annotated reports outside the repository; tests still never read `.local/`. |
| [ui-ux-pro-max](.claude/skills/ui-ux-pro-max/SKILL.md) | UI/UX decisions, accessibility, design systems, and targeted design-quality work. |
| [web-design-guidelines](.claude/skills/web-design-guidelines/SKILL.md) | Explicit reviews of web interface quality, accessibility, and UX guidelines. |

For overlapping interface work, prefer `frontend-design` for implementation,
`ui-ux-pro-max` for design-system or UX guidance, and `web-design-guidelines`
for compliance review. Skill instructions never override this file's security,
privacy, human-approval, and data-handling rules.

- `.claude/settings.json` holds the shared Claude Code permissions and is versioned; personal overrides go in `.claude/settings.local.json`, which Git ignores.
- In Claude Code, the Bash tool is Git Bash and the PowerShell tool is Windows PowerShell 5.1; run the project scripts with PowerShell.

## Code conventions

- Follow PEP 8.
- Prefer small functions with clear responsibilities.
- Add types to public interfaces.
- Use descriptive names in English.
- Handle all relevant errors explicitly.
- Do not use `except Exception` clauses unless necessary.
- Do not include secrets or specific personal paths.
- Write comments only for non-obvious logic.
- Add docstrings to important public functions.
- Keep UI-related functions separate from domain logic.

## Version control (GitFlow)

The repository follows GitFlow:

- `main`: only stable, released code. Each merge is tagged with its version.
- `develop`: integration branch. Receives finished features.
- `feature/<short-name>`: branches off `develop` and merges back into `develop` through a pull request.
- `release/<version>`: branches off `develop` to prepare a version. Only minor fixes are allowed, and it merges into `main` and then back into `develop` through pull requests.
- `hotfix/<short-name>`: branches off `main` to fix urgent production bugs. It merges into `main` and then into `develop` through pull requests.

Rules:

- Do not commit directly to `main` or `develop`, except for the repository's initial setup.
- Every change to `main` or `develop` goes through a pull request. Open it with `gh pr create` against the correct base (`develop` for features, `main` for releases and hotfixes).
- Group related implementation and controlled-test fixes into a coherent feature before proposing a pull request; do not open a new pull request for every small adjustment.
- Before creating a pull request, summarize the completed changes, actual validation results, and any unverified behavior, then ask the user whether it is time to open it. Create it only after explicit confirmation, with tested changes ready to version.
- Write pull request descriptions to a Markdown file and pass it with `gh pr create` or `gh pr edit` using `--body-file`. Do not pass multiline Markdown through a PowerShell-quoted `--body` argument, because escaping can corrupt line breaks and backtick-delimited text.
- Do not merge a pull request without the user's approval.
- After confirming a merge into `develop`, always fetch with `--prune`, switch to `develop` (if the worktree is clean) and fast-forward it to `origin/develop`, then clean up local feature branches whose tips are ancestors of the updated `origin/develop`, including the branch you were on. Use `git branch -d`, never force deletion. Preserve `main`, `develop`, branches checked out in other worktrees, and any branch with unmerged commits. If the worktree is not clean or deletion is refused, report it and leave the branch intact.
- Keep each commit focused on one coherent change. Use Conventional Commits alongside GitFlow: `<type>(<scope>): <description>`, with a descriptive Spanish description in the imperative mood. Use `feat` for new behavior, `fix` for bug fixes, `refactor` for behavior-preserving restructuring, `test` for tests only, `docs` for documentation or agent instructions, and `chore` for maintenance. GitFlow governs branches and merges; Conventional Commits governs commit messages. Do not rewrite previous commits merely to rename them unless explicitly requested.
- Run `pytest` before merging a feature.
- Commit messages include the `Co-authored-by` trailer of the assistant that made the change (Copilot or Claude), unless the user asks for it to be omitted.
- After every successful commit, always push the current GitFlow branch to `origin`; use `git push -u origin <branch>` if it has no upstream. Verify the remote branch matches local HEAD. If the push fails, report the error and leave the commit intact; never force-push to bypass rejection. Pushing does not authorize creating or merging a pull request.
- Do not rewrite history on shared branches (`main`, `develop`) or use `push --force` on them.

## Tests

Before finishing any significant change, run the smallest relevant test.

Tests must demonstrate:

- That nothing is written or moved without approval.
- That the approved data matches the data written.
- That the agent stops on UI, session and mailbox errors.
- That code generated by the model is not executed.
