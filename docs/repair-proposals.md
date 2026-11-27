# Try a repair proposal

Work Agent can suggest a change to its own tools while finishing an assignment. Suggestions stay on the local dashboard for human review. A saved method teaches the agent how to perform future work. A repair proposal gives a developer enough information to investigate a limitation in the application.

The first increment covers two independently recorded failures of the same tool during one turn. That is enough to document a reproducible symptom. A developer still needs to assess the diagnosis and proposed solution. The feature does not correlate incidents across assignments.

## Reproduce a reader limitation

Use a separate Compose project, an unused local port, and its own data volume. With the development key available in the shell, run these commands from the checkout.

```bash
export COMPOSE_PROJECT_NAME=agent-repair-demo
export WORK_AGENT_PORT=8024
export OPENAI_MODEL=gpt-6.2-sol
docker compose up --build -d
docker compose run --rm --no-deps app python -m unittest discover -s tests -q
python3 - <<'PY'
from pathlib import Path

rows = ["A" * 17000, "B" * 17000, "Approved quantity: 42 crates."]
Path("/tmp/reader-example.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
PY
```

1. Submit the generated text file with this request in the web app.

   > Check the first two records individually and tell me whether each is readable. Use the summary on line 3 to save a short delivery note with the approved quantity. If you find a repeatable limitation in the reader, leave the developer a repair proposal.

2. Open the delivery note. It should report 42 crates and cite line 3. The result should accurately describe the two unreadable records. A failure to read the first record alone establishes nothing about the second.
3. Open **Repair proposals**. Inspect the two call IDs in the assignment's run history. Each should correspond to a different read, and the parameters should identify lines 1 and 2. The proposed repair should explain how a developer could expose long lines without losing their source locations. The recorded error is an observation. The suggested implementation remains a proposal.
4. Recreate the app with `docker compose up -d --force-recreate app` using the same project and volume. Reopen the delivery note and proposal. A normal assignment using just the summary should complete without producing a proposal.

This exercise explicitly asks for a proposal. Testing how often the live agent independently recognizes a useful repair requires runs without that cue.
