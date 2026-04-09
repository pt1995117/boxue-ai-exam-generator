# Repo Rules For Coding Agents

This repository has a hard requirement: coding work must not leave runtime artifacts, caches, exports, or local analysis outputs in the git worktree.

## Non-Negotiable Rules

1. Before substantial work, run:
   `bash tools/check_worktree_hygiene.sh`

2. After code changes and before finishing, run:
   `bash tools/check_worktree_hygiene.sh`

3. Runtime and cache outputs must stay under:
   - `.local/runtime/`
   - `.local/cache/`

4. Do not create local working artifacts in the repository root.
   Examples:
   - `*.xlsx`
   - `*.xls`
   - `*.docx`
   - temporary reports
   - ad hoc dumps

5. Do not write generated runtime data into tracked tenant directories such as:
   - `data/*/audit/`
   - `data/*/mapping/`
   - `data/*/slices/`
   - `data/*/bank/`
   - `data/*/exports/`
   - `data/*/materials/uploads/`
   - `data/*/materials/references/`

6. Admin ports are pinned and must not drift:
   - backend: `8600`
   - frontend: `8522`

7. If a task requires temporary files, put them under:
   - `.local/tmp/`

8. If worktree hygiene fails, fix the artifact routing or local ignore strategy before ending the task.

## Notes

- `.githooks/pre-commit` blocks runtime/cache artifacts from entering commits.
- `.git/info/exclude` contains this machine's local artifact ignore layer.
- This file is here to make the rule explicit for future coding sessions in this repo.
