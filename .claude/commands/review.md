Review a pull request and provide structured feedback.

$ARGUMENTS

---

You are a senior engineer doing a thorough code review.

## Step 1 — Get the changes

Parse $ARGUMENTS to determine what to review:

**If $ARGUMENTS is a GitHub PR URL** (e.g. `https://github.com/org/repo/pull/123`):
- Extract owner, repo, and PR number from the URL
- Run: `gh pr diff <number> --repo <owner>/<repo>`
- Run: `gh pr view <number> --repo <owner>/<repo>` to read the PR description

**If $ARGUMENTS is a PR number** (e.g. `123` or `#123`):
- Run: `gh pr diff <number>`
- Run: `gh pr view <number>` to read the PR description
- (Uses the current repo)

**If $ARGUMENTS is a branch name**:
- Run: `git diff main..<branch>`
- Run: `git log main..<branch> --oneline`

**If $ARGUMENTS is empty**:
- Run: `git diff main`
- Run: `git log main..HEAD --oneline`
- (Reviews your own current changes)

Read every changed file in full before forming any opinion.

## Step 2 — Understand intent

Before commenting, answer these silently:
- What is this change trying to do?
- What would break if it shipped with a bug?
- What edge cases is the author likely to have missed?

## Step 3 — Structured review

---

### Summary

One paragraph: what this PR does, overall quality, and your recommendation:
**APPROVE** / **APPROVE WITH COMMENTS** / **REQUEST CHANGES**

### Critical issues (blocking)

Bugs, security issues, data loss risks, broken contracts — must fix before merge.
If none: write "None."

Format:
**`File.tsx:42`** — what the issue is and why it matters. Suggested fix.

### Non-critical issues (non-blocking)

Naming, missing edge cases, performance, readability — worth fixing but not blocking.
If none: write "None."

Format:
**`File.tsx:42`** — description and suggestion.

### Security check

- [ ] Hardcoded secrets, tokens, or credentials?
- [ ] SQL/NoSQL injection risks?
- [ ] Unvalidated user input reaching sensitive operations?
- [ ] Auth/permission checks missing?
- [ ] Sensitive data logged or exposed in errors?

### Test coverage check

- Happy paths tested?
- Error paths tested?
- Edge cases (null, empty, boundary values) tested?
- Anything untested that could silently break in production?

### Positive observations

What did the author do well? Be specific — this is not optional.

---

## DO NOT

- Do NOT approve changes you haven't fully read
- Do NOT nitpick style if a linter/formatter handles it
- Do NOT suggest rewrites without a concrete reason
- Do NOT be vague — every comment needs what the problem is AND why it matters