# One weekly responsibility

Use the existing worker and SQLite volume to run one active weekly responsibility. The user chooses a weekday and time in Pacific, provides the assignment, and can open its latest saved brief or stop future runs.

The worker queues a regular assignment and advances the schedule in one SQLite transaction. A scheduled occurrence has its own durable key. When multiple weeks were missed, the app queues the most recent due occurrence once and moves the next run to the next scheduled local time. A pending assignment uses the normal worker queue. Stopping the schedule leaves its runs and saved files available.

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
