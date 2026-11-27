# Try a repair proposal

Work Agent can suggest a change to its own tools while finishing an assignment. Suggestions stay on the local dashboard for human review. A saved method teaches the agent how to perform future work. A repair proposal gives a developer enough information to investigate a limitation in the application.

The first increment covers two independently recorded failures of the same tool during one turn. That is enough to document a reproducible symptom. A developer still needs to assess the diagnosis and proposed solution. The feature does not correlate incidents across assignments.

## Verify the long-line repair

The two live assignments on PR #21 cited a real limitation. Whole-line reads stopped at 16,000 characters. Work Agent now has an additional tool, `read_line_range`, that reads up to 4,000 Unicode code points from one numbered line. It returns the starting and ending offsets, line length, continuation offset, and the original line citation. The same tool works on current uploads, saved artifacts and pinned sources from earlier assignments.

From this checkout, start an isolated Compose project using the development key already configured in the environment.

```bash
export COMPOSE_PROJECT_NAME=agent-line-range-demo
export WORK_AGENT_PORT=8025
export OPENAI_MODEL=gpt-6.2-sol
docker compose up --build -d
docker compose run --rm --no-deps app python -m unittest discover -s tests -q
docker compose exec -T app python -m tests.test_line_range_workflow /tmp/records.txt
docker compose cp app:/tmp/records.txt /tmp/records.txt
```

1. Upload `/tmp/records.txt` through the web form and ask “How many crates were approved across these two delivery records? Save a short delivery note showing the combined quantity and citing each record.”
2. Verify that the file contains exactly two records. Inspect the saved note and run history. The two quantities should come from reads covering character positions beyond 16,000. Trace the 17 and 25 inputs to separate original line URLs and check that the calculation gives 42 crates.
3. Recreate the service with `docker compose up -d --force-recreate app`. Reopen the note and follow both links. Verify they select lines 1 and 2 of the original file. Compare the downloaded original with the uploaded bytes.

## Reproduce the original limitation at PR #21

Use commit `252f95a` for this historical exercise. Its third-line summary provided the workaround.

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
