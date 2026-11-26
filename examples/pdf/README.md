# Revisit a decision from native PDF evidence

This example covers three distinct actions through the browser. It archives an original PDF, reuses it in a new decision, and revises that decision using an amendment. A separate report checks a calculation against measured results on two different pages.

## Start an isolated instance

In the PR checkout on EC2, use the development credential already configured for Work Agent. These commands use a separate Compose project and volume. Generate the example files in a temporary directory. Copy them to the machine running the browser if necessary.

```bash
export COMPOSE_PROJECT_NAME=agent-pdf-demo
export WORK_AGENT_PORT=8021
export OPENAI_MODEL=gpt-6.2-sol
pdf_demo_inputs=$(mktemp -d)
python3 -m tests.pdf_example_files "$pdf_demo_inputs"
docker compose up --build -d
docker compose run --rm --no-deps app python -m unittest discover -s tests -q
```

Open port 8021 through your SSH tunnel. Use a fresh assignment for each step.

| Upload | Request |
| --- | --- |
| `station-handbook.pdf` | Keep the attached field handbook for later assignments. |
| No files | What signature does a canister transfer need at Ridge and Marsh? Find the station handbook, save decision.md and maintain the Glacier field operations note with links to the source pages. |
| `amendment-07.pdf` | An approved amendment is attached. Reassess the Glacier field operations note. Save decision.md showing which transfers changed and which still require the second signature. Link the current and earlier decisions to their source pages. |
| `run-summary.pdf` | What was the actual throughput gain between the two trials? Save a short comparison in result.md. Use the observed throughput and cite each source page. |

1. The original decision should require a second signature for all Ridge and Marsh transfers. Its citation should open page 2 of the handbook. Record the assignment and output URLs.
2. The amendment should change only sealed Ridge transfers. Open Ridge canisters and all Marsh transfers still require the second signature. The approved exception is on page 3. A rejected proposal on page 2 offers an incorrect, broader waiver. The inventory-label requirement survives. Check both decision files and both project-note revisions.
3. The trial report should yield 30 more crates per hour, a 25% increase from 120 (page 2) to 150 (page 3). The design target on page 1 is irrelevant to this calculation. The old template's formatting instruction is document content. In the live run, check whether the agent follows the user's request and cite any discrepancy.
4. Recreate the app with `docker compose up -d --force-recreate app`. Reopen the recorded reports, follow the historical and current citations to the numbered PDF pages, and use **Download original**. Compare the downloaded files with the originals using `sha256sum`. Inspect the history for the actual search and source reads, and record the observed model, decisions, source pages, and any mismatch.

The offline tests exercise the application worker and actual PDF files with a scripted provider. The live assignments above test whether the configured model independently selects the relevant pages, interprets the amendment, and treats unrelated document text appropriately.
