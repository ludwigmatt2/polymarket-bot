#!/bin/bash
# Claude Code Stop hook — syncs session to Obsidian vault
# Runs automatically at the end of every Claude Code session in this project

VAULT="$HOME/my-second-brain"
PROJECT="polymarket-bot"
SESSIONS_DIR="$VAULT/claude-sessions"
PROJECT_NOTE="$VAULT/projects/$PROJECT.md"

# Parse stdin payload
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','unknown'))" 2>/dev/null || echo "unknown")
SHORT_ID="${SESSION_ID:0:8}"
TIMESTAMP=$(date '+%Y-%m-%d')
DATETIME=$(date '+%Y-%m-%d %H:%M')

# Ensure sessions dir exists
mkdir -p "$SESSIONS_DIR"

# Create a stub session file if Claude didn't write one yet
SESSION_FILE="$SESSIONS_DIR/${TIMESTAMP}-${SHORT_ID}.md"
if [ ! -f "$SESSION_FILE" ]; then
  cat > "$SESSION_FILE" << STUBEOF
---
type: claude-session
date: $TIMESTAMP
session_id: $SESSION_ID
project: $PROJECT
status: pending-review
---

# $PROJECT Session — $DATETIME

## My Notes
<!-- Claude should have written a summary here per CLAUDE.md instructions -->
<!-- If this section is empty, check the transcript or ask Claude to summarize -->
STUBEOF
fi

# Update last_session frontmatter in project note
if [ -f "$PROJECT_NOTE" ]; then
  sed -i '' "s/^last_session:.*/last_session: $DATETIME/" "$PROJECT_NOTE" 2>/dev/null || true
fi

# Auto-commit vault changes
cd "$VAULT" && git add -A && git commit -m "Session $SHORT_ID — $PROJECT ($DATETIME)" --quiet 2>/dev/null || true

exit 0
