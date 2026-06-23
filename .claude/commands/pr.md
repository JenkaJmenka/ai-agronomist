Prepare and write a pull request for my current changes.

$ARGUMENTS

---

You are a senior engineer preparing a pull request. You will do TWO things in order:
first review your own changes critically, then write the PR description.

## Phase 1 — Self-review

Run `git diff main` to see all changes.
Run `git log main..HEAD --oneline` to see your commits.
Read every changed file in full.

Then answer these silently before writing anything:
- What is this change actually doing?
- Is there anything I would flag if someone else submitted this?
- Are there missing tests, edge cases, or error paths?
- Is there anything that could break in production?
- Are there any security concerns (hardcoded secrets, unvalidated input, missing auth checks)?

If you find issues, list them as **Pre-PR issues to fix** before the description.
Do not write the PR description until the code is worth merging.

## Phase 2 — PR description

Once the code is clean, write the description using this exact structure.
Do not skip or merge sections.

---

### Problem statement

What problem does this PR solve? Why now?
Write from the reviewer's perspective — what context do they need?

### Approach

How was it solved? Why this approach over alternatives?
If other approaches were considered and rejected, explain why.

### Changes

Bullet list of specific files and what changed:
- `FileName.tsx` — what changed and why
- `util.ts` — what changed and why

### Testing

- Unit tests added or modified
- Integration/E2E tests if applicable
- Manual testing steps a reviewer can reproduce
- Edge cases explicitly covered

### Deployment considerations

- [ ] Database migration: yes/no — if yes, describe
- [ ] Feature flag: yes/no — if yes, name and default state
- [ ] Environment variables: yes/no — if yes, list them
- [ ] Cache invalidation: yes/no — if yes, describe
- [ ] Rollback plan: how to revert if this causes issues

### Screenshots

[attach before/after screenshots for UI changes — omit if backend-only]

---

## DO NOT

- Do NOT write the description before reviewing the code
- Do NOT use vague language ("various fixes", "misc improvements")
- Do NOT omit the deployment section even if all answers are "no"
- Do NOT skip screenshots for UI changes