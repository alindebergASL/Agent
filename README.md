# Work Agent

Work Agent accepts a request and uploaded text files, runs a background assignment, and saves the resulting CSV and Markdown files for you to reopen.

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

## Run offline tests

```bash
docker compose run --rm --build --no-deps app python -m unittest discover -s tests -v
```

The tests run with a model double and require no API key. The initial image build downloads Python packages.

## Run the quote demonstration

Upload both CSVs from [examples/quotes](examples/quotes) and submit the request in [the first-task brief](docs/first-task.md). When the assignment completes, open its CSV comparison and Markdown recommendation. Recreate the container from the same Compose project, then reopen the files from the assignment list and submit a fresh request.

```bash
docker compose up -d --force-recreate app
```

Compose mounts the `agent-data` volume at `/data` for the assignment database, inputs, and outputs. Keep the volume when recreating the container or updating the app.

Open **Run history (JSON)** from a task's Activity section to inspect its saved Responses items and tool receipts. Hosted calls share a program ID, and Activity shows the file and line range each program read. The history can include uploaded text returned by a tool, so review it before sharing.

### Continue a completed assignment

Open the saved recommendation, choose **Edit Markdown**, and save your changes. Return to the assignment and submit a follow-up. The app reuses the original uploads and conversation. The existing file links open the latest saved revision, and the revision list lets you reopen earlier versions. Markdown source links open the numbered rows of the original uploads.

The agent reads the saved revisions that were current when its turn started. If you edit one of the files it is replacing before it finishes, the app keeps your edit and leaves that turn’s draft outputs unpublished. Submit a follow-up to retry against the saved revisions.

### Change course during a turn

Choose **Stop** to stop the current turn. You can send a fresh instruction on the same assignment and reuse its uploads and saved files. To amend a running request, choose **Send correction**. The app records the correction immediately.

Once “Waiting for the model” appears in the activity log, the worker has committed to that provider request, including any hosted searches it performs. If you stop or correct the turn at that point, the worker discards the result when it returns. The old turn can no longer run an application tool or publish a draft. The worker can then pick up the next queued turn.

Closing the browser leaves the app working. If the app restarts during a turn, reopen the assignment and choose **Continue**. The app queues the interrupted request against the original uploads and the current saved revisions. The interrupted turn and its recorded steps stay in the history, and published artifacts remain available at their existing links.

### Reuse a verified method

Check the result of a completed assignment, then choose **Save reusable method**. Give it a short title, describe when it applies, and write the procedure and the evidence you checked. Keep prices, decisions, and notes specific to that assignment out of the reusable procedure.

The **Saved methods** page lets you inspect, edit, or remove a method and open its source assignment. New runs see a short index of the eight most recently updated methods. When the agent retrieves one, its activity and run history show the retrieval. The agent gets the procedure and scope while the evidence and source assignment remain available to you from the method page.

The quote demonstration can also run against the offline Responses fixture. Use a separate Compose project so its saved assignments and model configuration stay separate from live runs.

```bash
export COMPOSE_PROJECT_NAME=work-agent-demo
docker compose -f compose.yaml -f tests/compose.fixture.yaml up --build -d
docker compose -f compose.yaml -f tests/compose.fixture.yaml up -d --force-recreate app
```

Submit the two-shipment request and open both saved files before running the recreation command. After recreation, reopen them and submit the same request with one shipment. The fixture supports those two prompts and computes its outputs from the uploaded rows and arithmetic tool results.
