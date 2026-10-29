# Dev Ship

Use when the operator asks to build, fix, refactor, test, review, or ship code.

## Inspect first
1. `dev` code: `git_status`, list the tree, read the README and the files in scope.
2. Find the test/lint commands before editing.
3. Check for uncommitted user changes; never overwrite them blindly.

## Work
- Plan three to six steps and state them briefly.
- Make the smallest change that solves the problem; follow local style.
- Run the relevant tests or linter after each logical change.
- Use `dev` session for long builds; `dev` github for issues, PRs, and repo operations.
- On failure, read the real error, fix the cause, and retry once before reporting.

## Approvals
- T1: read, search, run tests, edit inside the workspace.
- T2: install packages, edit outside the workspace, git commit, open a PR, publish.
- T3: force-push, rewrite history, delete branches or files, change credentials.

## Finish
Report: files changed, tests run, pass/fail, and remaining risk. Never commit secrets; check `.gitignore` first. Do not claim a test passed unless the command output says so.
