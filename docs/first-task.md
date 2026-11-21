# First Codex Cloud task

Build the first working slice of the Work Agent blueprint. A single user opens a small web interface, uploads the two sample supplier quotes, enters a request, and later reopens a saved CSV comparison and Markdown recommendation through the interface. Read docs/blueprint.md and AGENTS.md. Limit this pull request to that outcome.

Use Python, FastAPI, SQLite, and server-rendered HTML as the starting stack unless the repository already gives a good reason to use something else. Use an application-owned OpenAI Responses loop with direct tools for reading uploaded files, calculating values, and producing artifacts. Keep the implementation general. Use a configurable data directory for SQLite, inputs, and saved outputs. Keep original inputs intact and let the application publish staged results. Run any generated code in isolation if you introduce that capability. A bounded arithmetic tool is sufficient here.

## Container setup

Include a Dockerfile, a .dockerignore, and a compose.yaml in this PR. Install dependencies in the image and run the application as a non-root user. Define one service named app, with a named volume mounted at /data for SQLite, inputs, and outputs. Bind the published port to localhost. Supply the model credential at runtime and keep local credentials and application data out of Git and the image. Set a restart policy suitable for a single EC2 host.

Support a clean-checkout start with `docker compose up --build -d` and document a Compose command for the offline tests. After the upload demonstration, recreate the app with `docker compose up -d --force-recreate app` using the same volume. Reopen the comparison and recommendation, confirm their contents, and submit a fresh request. If the Codex Cloud environment cannot run Docker, run the application checks there and use a Docker-capable CI runner for the Compose check, or identify exactly what remains unverified. Keep the first calculation tool bounded. If arbitrary generated code is added later, give it a disposable sandbox, scoped staged files, and no application credentials or host Docker socket.

## Demonstration

Upload only examples/quotes/supplier-a.csv and examples/quotes/supplier-b.csv into the running application. Use this request:

> Compare the cost of buying ten units delivered in two shipments. Give me a CSV and a short recommendation. Show where each price and freight term came from, and tell me whether changing the number of shipments could change the choice.

The following values are independent verification data for the builder. Keep the task brief and expected results out of the application's model context. With two shipments, A is $1,060 and B is $1,050. With one shipment, A is $1,030 and B is $1,050. Reopen both saved outputs, check the arithmetic and source rows, and show the recommendation changes when the same ten units are delivered together.

## Pull request

Document a start command, how to run the checks, and how to configure the application model for a live run. Keep repeatable tests usable offline. Report which checks used a live model and which used a test double. If development credentials are unavailable, finish the implementation and offline checks, and identify the exact live check still outstanding. Include the actual comparison and links or paths to the generated outputs in the PR summary. Open a pull request for review.

Hosted programmatic tool calling and the unrelated research brief follow in the next task. Continuity, learning transfer, recurring work, and EC2 deployment come in later slices.
