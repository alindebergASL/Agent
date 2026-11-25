# One weekly responsibility

Use the existing worker and SQLite volume to run one active weekly responsibility. The user chooses a weekday and time in Pacific, provides the assignment, and can open its latest saved brief or stop future runs.

The worker queues a regular assignment and advances the schedule in one SQLite transaction. A scheduled occurrence has its own durable key. When multiple weeks were missed, the app queues the most recent due occurrence once and moves the next run to the next scheduled local time. A pending assignment uses the normal worker queue. Stopping the schedule leaves its runs and saved files available.

On catch-up, the dashboard shows when the occurrence was scheduled. The assignment uses the Pacific date when it was queued to interpret “the past week.” The dashboard links to the Markdown files from the first completed turn that produced them for that occurrence. Those links open the current revisions, including later edits. If the next run is pending or fails, the previous brief remains available.

When the worker first starts an occurrence, it selects the most recent earlier occurrence of this responsibility that published a brief. It records the exact revision of each Markdown file in that brief, including edits saved before the worker started. The `read_prior_artifact` tool gives the assignment read-only access to those revisions. Later edits and app restarts preserve its original reference. An intervening run that failed to publish a brief is skipped. The first run establishes a baseline.

## Compare three runs

Start the [read-only GitHub source](../examples/github-status/README.md) in an isolated project. From the repository root, use a shell with the app’s development OpenAI key configured.

```bash
export COMPOSE_PROJECT_NAME=agent-recurring-brief
export WORK_AGENT_PORT=8016
export OPENAI_MODEL=gpt-6.2-sol
export WORK_AGENT_GITHUB_REPOSITORY=alindebergASL/Agent
docker compose -f compose.yaml -f examples/github-status/compose.yaml up --build -d
```

Open `http://127.0.0.1:8016` through your EC2 SSH tunnel. On **Weekly responsibility**, choose a future weekly time and save this request.

> Check open pull requests and the most recent merge in alindebergASL/Agent. Save a dated Markdown brief with what changed, which commits have reported live verification, and what needs a decision. Compare against the previous brief for this responsibility when available. Recheck its unresolved items. Cite the GitHub records and captured evidence. If nothing material changed, say what you checked. If a source was unavailable, say which part you could not verify.

1. Select **Run now**. After the run completes, open the first brief and check its PR numbers and cited evidence against GitHub.
2. Record an actual verification result in a GitHub comment on a PR covered by the first brief, or use a new merge in that repository. Select **Run now** again. Open the new assignment and confirm it identified the change from fresh source data and linked to the exact first brief revision it used.
3. Once the second run completes, select **Run now** again during a quiet interval. Verify that the third assignment used the second brief and checked GitHub before reporting whether anything changed. Open the earlier briefs and confirm their revisions and evidence links still work. In **Run history (JSON)**, look for the prior artifact reads and the new source reads.

Stop the demo from the same shell when finished. Its named volume retains the assignments for review.

```bash
docker compose -f compose.yaml -f examples/github-status/compose.yaml down
```

## Reproduce

Open **Weekly responsibility**, select Friday and 9:00 a.m. Pacific, and save this request.

> Save a short brief on worthwhile new agentic AI papers and reports from the past week. For each pick, link the primary source and tell me what changed, why it matters in practice, and what to read first.

The live demonstration uses the same request with a Pacific time a few minutes ahead. Close the browser after saving, then reopen the page after the scheduled time and check the dated brief and next run.

```bash
docker compose up --build -d
docker compose run --rm --build --no-deps app python -m unittest discover -s tests -v
docker compose up -d --force-recreate app
```

## Verification

All 37 offline tests passed in Docker Compose in Codex Cloud. The controlled clock checked Friday at 9:00 a.m. across the change from PDT to PST, no early enqueue, one catch-up assignment after missed weeks, and deduplication across concurrent checks and restart. The dashboard test reopened the dated Markdown and checked that Stop prevents another occurrence.

In the browser, the Friday setup showed November 27, 2026 at 9:00 a.m. PST. The near-term live slot was November 24 at 8:47 a.m. PST. I closed Chromium and recreated the app with the same volume before the due time. The worker created the assignment at 8:47:00.567 a.m., before the first post-due HTTP check at 8:47:15 a.m.

The live `gpt-6.2-sol` run saved a brief dated November 24, 2026. Its five primary-source links matched completed hosted research actions in the run history. Direct HTTPS to those sources returned cloud proxy 403, so the citation check used the hosted trace. The dashboard showed the next run at 8:47 a.m. PST on December 1. I stopped the demo schedule through the app and verified its brief remained linked.
