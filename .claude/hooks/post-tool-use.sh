#!/bin/bash
# PostToolUse — auto-format after edits
FILE="$CLAUDE_FILE_PATH"
[ -z "$FILE" ] || [ ! -f "$FILE" ] && exit 0

EXT="${FILE##*.}"

case "$EXT" in
  js|jsx|ts|tsx|css|scss|json|html|md|mdx|yaml|yml)
    command -v npx &>/dev/null && npx prettier --write "$FILE" 2>/dev/null || true ;;
  py)
    if command -v ruff &>/dev/null; then ruff format "$FILE" 2>/dev/null || true
    elif command -v black &>/dev/null; then black --quiet "$FILE" 2>/dev/null || true; fi ;;
  go)
    command -v gofmt &>/dev/null && gofmt -w "$FILE" 2>/dev/null || true ;;
  rs)
    command -v rustfmt &>/dev/null && rustfmt "$FILE" 2>/dev/null || true ;;
esac

exit 0
