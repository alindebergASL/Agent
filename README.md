# Work Agent

Work Agent accepts a request and uploaded PDFs or text files, runs a background assignment, and saves the resulting CSV and Markdown files for you to reopen.

The app can batch independent file reads in an OpenAI-hosted program and still use direct calls for individual operations. Both paths use the same file reader. Calculations and artifact writes use direct calls. For public research, the hosted web tool can search and open source pages. Ask for a saved brief with links to the pages that support its claims.

## Start the app

Run from the checkout with Docker and Docker Compose. In Bash, enter your development API key when prompted. Compose supplies the key to the running service.

```bash
read -rsp 'OpenAI API key: ' OPENAI_API_KEY
printf '\n'
export OPENAI_API_KEY
docker compose up --build -d
```

Open `http://127.0.0.1:8000`. The app uses `gpt-6.2-sol` by default. To use Terra, run `OPENAI_MODEL=gpt-6.1-terra docker compose up --build -d`. Set `WORK_AGENT_PORT` to change the local port. `OPENAI_BASE_URL` is optional and can point the app to a local Responses test double.

## Read from connected systems

Work Agent can discover selected read tools from HTTP MCP servers and call them directly or from OpenAI-hosted JavaScript. A program can combine those records with uploaded files and saved revisions in the same assignment. Each successful connected read records the returned data on the assignment. A citation in a saved Markdown report can reopen that snapshot after the upstream data changes.

Two examples run through the normal assignment page:

- Ask which purchase orders are short across purchasing and warehouse. Correct one upstream receipt and verify both reports. [Purchasing and warehouse example](examples/connected-sources/README.md)
- Ask “How are we doing, and what’s next?” about a public GitHub repository. Save a status brief, then use the same assignment to ask what changed. The source reads pull requests, comments and reviews without a GitHub token. [GitHub status brief](examples/github-status/README.md)

To connect your own system, mount a JSON file read-only into the app container and set `WORK_AGENT_MCP_CONFIG` to its container path. List the exact tool names the program may call. Each tool must advertise `readOnlyHint: true` and omit or disable `destructiveHint`. Use a credential with read access to the intended data. Supply its value to the app through the environment variable named by `token_env`. Bearer authentication requires HTTPS.

```json
{
  "sources": [{
    "name": "warehouse",
    "url": "https://warehouse.example.com/mcp",
    "tools": ["list_receipts"],
    "token_env": "WAREHOUSE_READ_TOKEN"
  }]
}
```

For example, save that config as `sources.json` beside `compose.yaml` and add a local Compose override. Set `WAREHOUSE_READ_TOKEN` in the shell that starts the app.

```yaml
services:
  app:
    environment:
      WORK_AGENT_MCP_CONFIG: /run/work-agent/sources.json
      WAREHOUSE_READ_TOKEN:
    volumes:
      - ./sources.json:/run/work-agent/sources.json:ro
```

Selected tool descriptions and the returned records are sent to the configured model provider as part of the assignment. The app stores the results in its existing volume. A linked snapshot represents one read. Consistency across multiple pages depends on the upstream system’s paging contract.

## Run offline tests

```bash
docker compose run --rm --build --no-deps app python -m unittest discover -s tests -v
```

The tests run with a model double and require no API key. The initial image build downloads Python packages.

## Run a weekly responsibility on demand

Open **Weekly responsibility** and choose **Run now** to queue an assignment using the saved request. The next weekly run stays at its scheduled time. If an on-demand assignment is still queued, running, or waiting for your input, **Run now** opens that assignment.

Each new occurrence can read the revision of the previous brief that was current when its worker started. Ask the responsibility to compare fresh sources with that brief and report material changes. [Try three consecutive runs on GitHub data](docs/weekly-responsibility.md#compare-three-runs).

## Watch a decision or look for something new

Open **Watches** to describe what to look for and when it deserves your attention. Each watch has its own daily or weekly Pacific schedule. Use **Check now** for an immediate check. To track a particular saved file or project note, start the watch from that item's page. You can also specify public pages that every successful check must visit.

For example, one watch can follow changes to a return policy while another looks for grants your project qualifies for. Each check can use current project claims and compare fresh source evidence with its own earlier findings. A qualifying opportunity can appear on the first check. Open **Needs your attention** on the dashboard to see what changed and why. **Mark reviewed** preserves the finding in history. A source outage appears in the check history and preserves the last verified result. **Stop** ends future checks on that watch.

Connected-source citations reopen the snapshot saved at check time. Public web citations keep the URL and visit record and open the publisher's current page. Shared note revisions support citations to exact uploads, saved revisions and connected-source snapshots.

[Try the two-watch example](examples/watches/README.md) · [How the agent uses memory before acting](docs/knowledge-foundation.md)

## Work with PDF sources

Upload a searchable PDF with an assignment, a follow-up, or an answer to a clarification. A later assignment can find and cite the same original. A citation opens the physical page with its rendered image and numbered text. **Download original** returns the uploaded bytes, including the original layout. Physical page 1 is the first page in the file, even if the document labels it as a cover or uses Roman numerals.

The app extracts text locally and stores it by page in the existing SQLite volume. The agent reads that text. The page image lets you check the layout, charts, and figures yourself. Textless pages in a mixed document are identified in the reader. For an image-only scan, supply an OCR-processed copy. Unlock encrypted files before uploading them. PDFs may be up to 16 MiB and 500 pages, with a 4 MiB extracted-text limit.

[Try a policy amendment and a cross-page calculation](examples/pdf/README.md).

## Find a file from earlier work

To reuse earlier work, describe an original upload or generated file by name, assignment, or a passage you remember. The agent can find the passage, open the exact version and cite its rows. You can also start a new assignment directly from an open upload or saved revision. [Try the meeting-brief, CSV, and original-quote examples](docs/find-saved-work.md).

## Revisit a decision

Ask the agent to maintain a shared project or account note and cite the evidence behind each claim. In a fresh assignment, provide an amendment or conflicting source and ask what changes, what still holds, and why. Open **Project notes** to read or edit the current decision and follow a superseded claim back to its sources. An upload's **Where this original was used** link and a saved file's **Where this revision was used** link trace the work that cited it. [Knowledge foundation and verification](docs/knowledge-foundation.md).

## Run the quote demonstration

Upload both CSVs from [examples/quotes](examples/quotes) and submit the request in [the first-task brief](docs/first-task.md). When the assignment completes, open its CSV comparison and Markdown recommendation. Recreate the container from the same Compose project, then reopen the files from the assignment list and submit a fresh request.

```bash
docker compose up -d --force-recreate app
```

### Where the app stores files

Compose mounts the named volume `agent-data` at `/data`. Each Compose project normally gets its own volume. Recreating the app container with the same volume preserves assignments and files.

| Data | Location and identity |
| --- | --- |
| Original uploads | SQLite database `/data/agent.sqlite3`, table `uploads`. Each record contains the original bytes, filename, upload position, and links to the assignment and turn. Within an assignment each filename is unique. |
| PDF page text | The same database, table `pdf_upload_pages`. Each page records the physical page number, its extracted text, and a stable range of numbered lines. |
| Generated files | The same database, table `artifact_revisions`. Each published version has an assignment, filename, revision number, author, and content. A user edit creates another revision. |
| Reused files | The consuming assignment holds a reference back to the original upload or a specific generated revision. The same filename can exist in several assignments. |
| Early installations | An upload from the first storage format may still have its bytes at `/data/runs/<assignment-id>/inputs/<position>`. The database records the position and the reader supports both formats. |

On the production EC2 deployment, this command prints the host directory for its named volume. For an isolated test project, substitute its actual volume name.

```bash
docker volume inspect --format '{{ .Mountpoint }}' agent_agent-data
```

Open **Run history (JSON)** from a task's Activity section to inspect its saved Responses items and tool receipts. Hosted calls share a program ID, and Activity shows the file and line range each program read. The history can include uploaded text and records from connected systems, so review it before sharing.

### Continue a completed assignment

Open the saved recommendation, choose **Edit Markdown**, and save your changes. Return to the assignment and submit a follow-up. The app reuses the original uploads and conversation. You can attach new sources with a follow-up. Each source needs a distinct filename so earlier citations keep pointing to the original bytes. The existing file links open the latest saved revision, and the revision list lets you reopen earlier versions. Markdown source links open the numbered rows of the original uploads.

The agent reads the saved revisions that were current when its turn started. If you edit one of the files it is replacing before it finishes, the app keeps your edit and leaves that turn’s draft outputs unpublished. Submit a follow-up to retry against the saved revisions.

### Answer a question from the agent

When the agent needs a missing fact, source, or decision to finish, the assignment can show **Waiting for your input**. The worker is free to run other assignments. Reopen the same assignment after a restart and answer with text, supporting files, or both. The agent receives the original request, its question, your reply, and the available sources. The run history keeps the earlier tool calls. Only completed turns publish their draft files and methods.

For example, upload a unit price and ask for a firm delivered total when only you know the freight charge. When asked, attach the freight terms. The finished calculation and both sources are available in the same assignment.

### Change course during a turn

Choose **Stop** to stop the current turn. You can send a fresh instruction on the same assignment and reuse its uploads and saved files. To amend a running request, choose **Send correction**. The app records the correction immediately.

Once “Waiting for the model” appears in the activity log, the worker has committed to that provider request, including any hosted searches it performs. If you stop or correct the turn at that point, the worker discards the result when it returns. The old turn can no longer run an application tool or publish a draft. The worker can then pick up the next queued turn.

Closing the browser leaves the app working. If the app restarts during a turn, reopen the assignment and choose **Continue**. The app queues the interrupted request against the original uploads and the current saved revisions. The interrupted turn and its recorded steps stay in the history, and published artifacts remain available at their existing links.

### Reuse a verified method

Check the result of a completed assignment, then choose **Save reusable method**. Give it a short title, describe when it applies, and write the procedure and the evidence you checked. Keep prices, decisions, and notes specific to that assignment out of the reusable procedure.

The **Saved methods** page lets you inspect, edit, or remove a method and open its source assignment. For each turn, the worker searches all method titles and scopes and ranks matches by relevance. The model can page through matching summaries, then read a procedure that fits. When the agent retrieves one, its activity and run history show the retrieval. The agent gets the procedure and scope while the evidence and source assignment remain available to you from the method page. [Method selection and paired evaluation](docs/method-selection.md)

The quote demonstration can also run against the offline Responses fixture. Use a separate Compose project so its saved assignments and model configuration stay separate from live runs.

```bash
export COMPOSE_PROJECT_NAME=work-agent-demo
docker compose -f compose.yaml -f tests/compose.fixture.yaml up --build -d
docker compose -f compose.yaml -f tests/compose.fixture.yaml up -d --force-recreate app
```

Submit the two-shipment request and open both saved files before running the recreation command. After recreation, reopen them and submit the same request with one shipment. The fixture supports those two prompts and computes its outputs from the uploaded rows and arithmetic tool results.

### Excel and Word evidence

Upload a macro-free `.xlsx` workbook or `.docx` document with an assignment or follow-up. The original download is preserved. Each source link opens a frozen line labeled with its worksheet and cell, Word body paragraph, or Word table row and column. A later assignment can discover the original through saved-work search. Reopen the cited line to inspect the supporting value in context.

Excel shows stored values, cell types, number formats, and formula expressions. Formulas are unevaluated. Use literal inputs to reproduce a calculation. Word extraction covers body paragraphs and ordinary tables. Headers, footers, tracked revisions, drawings, and embedded images are outside that coverage. Use a searchable PDF when the decision depends on the printed page. For legacy binary `.doc`, save a copy as `.docx` or export a searchable PDF.

**Live acceptance on an isolated Compose project.** Generate sample uploads with `python -m tests.test_office_documents /tmp/office-demo` in the app environment. Upload `original/budget.xlsx` and ask whether ten units fit a $100 cap, requesting a saved decision with sources. In a fresh assignment upload `revised/budget.xlsx` and ask for the current and previous decisions with citations. Expected totals are $80 and $120. Open each rate citation and check that it selects the corresponding Rates!B1. Upload `policy.docx` and ask when general reviews occur and which visits qualify for the exception. Check that the two citations open the body paragraph and table cell, respectively. Recreate the isolated app container with its volume and reopen the decisions and original downloads.
