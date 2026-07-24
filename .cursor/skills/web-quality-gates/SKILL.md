---
name: web-quality-gates
description: >-
  Prevent broken web commits and pushes. Use before committing or pushing any
  change under web/, after editing TypeScript/React files, or when fixing CI
  lint/typecheck/unit failures. Enforces biome, tsc, and vitest gates that
  match web-ci.
---

# Web quality gates (prevent broken commits)

## When this applies

- Any edit under `web/`
- Before `git commit` or `git push` that includes `web/` files
- When fixing CI job `web-ci` / biome lint failures

## Required checks (run from `web/`)

Use `corepack pnpm` (never pnpm 11):

| Gate | Command | Must pass before |
|------|---------|------------------|
| Lint | `corepack pnpm lint` | commit (staged) / push |
| Auto-fix | `corepack pnpm lint:fix` | after edits if biome complains |
| Types | `corepack pnpm typecheck` | push |
| Unit | `corepack pnpm test` | push |

Husky already runs:

- **pre-commit** — lint-staged biome on staged `web/**/*.{ts,tsx,js,jsx,json}`
- **pre-push** — full `lint` + `typecheck` + `test` in `web/`

Do **not** skip hooks (`--no-verify`) unless the user explicitly asks.

## Biome footguns that fail CI

- Import order: `type` imports before values — `{ type RefObject, useEffect }` not `{ useEffect, type RefObject }`
- Format: run `lint:fix` rather than hand-editing whitespace
- After adding/editing files, run `corepack pnpm lint` from `web/` before committing

## Agent checklist before finishing web work

1. `cd web && corepack pnpm lint:fix`
2. `corepack pnpm lint` (must exit 0)
3. If committing: stage only intended files; let husky pre-commit run
4. If pushing: husky pre-push must pass; if it fails, fix and push again — do not `--no-verify`
