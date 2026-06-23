Generate comprehensive tests for: $ARGUMENTS

---

You are a senior engineer writing tests for a production TypeScript codebase.

## Target

Function or module: **$ARGUMENTS**

Read the implementation file first, then write tests.

## Requirements

- Match the test framework already used in this project (check existing test files)
- Group with `describe('$ARGUMENTS', ...)` blocks
- Name each test `it('should ...')`
- Use `beforeEach` / `afterEach` for setup
- Mock all external dependencies — no real network calls or DB queries
- Type all mocks correctly — no `as any`

## Coverage required — go beyond happy path

1. **Boundary values** — min, max, empty, zero, negative
2. **Null and undefined inputs** — each nullable param tested independently
3. **Type coercion edge cases** — `"0"` vs `0`, empty string vs undefined
4. **Error paths** — every thrown error, rejected promise, caught exception
5. **Side effects** — verify what is called, not just what is returned
6. **Async behavior** — race conditions, timeout, retry if applicable

## DO NOT

- Do NOT test implementation details or private methods
- Do NOT write tests that duplicate the implementation logic
- Do NOT mock the function under test itself
- Do NOT skip edge cases just to hit a coverage number

## Deliverable

One test file alongside the implementation.
