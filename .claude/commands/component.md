Generate a React component following our codebase conventions.

**Component name:** $ARGUMENTS

---

You are a senior frontend engineer generating a React component for a production codebase.

## Component specification

**Name:** $ARGUMENTS
**Purpose:** [describe what this component does and why it exists]
**Location:** src/components/$ARGUMENTS.tsx

## Requirements

- Props interface named `$ARGUMENTSProps`
- Named export only — no default exports
- Tailwind utility classes only — no inline styles, no CSS modules
- All event handlers typed with React event types — no `any`
- Handle loading, error, and empty states explicitly
- Accessible: correct ARIA roles, keyboard navigation, focus management
- No hardcoded copy — all user-visible strings via props

## DO NOT

- Do NOT use inline styles (`style={{}}`)
- Do NOT use `any` as a type
- Do NOT use default exports
- Do NOT skip the test file
- Do NOT add unnecessary state — keep it as controlled as possible

## Deliverables

Produce TWO files:

**File 1: `src/components/$ARGUMENTS.tsx`**

**File 2: `src/components/$ARGUMENTS.test.tsx`**
Using `@testing-library/react`. Include:
- Render test
- Props test
- Interaction test (if applicable)
- Accessibility test using role queries
