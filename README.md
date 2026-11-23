# Work Agent

Work Agent accepts a request and uploaded text files, runs a background assignment, and saves the resulting CSV and Markdown files for you to reopen.

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

The quote demonstration can also run against the offline Responses fixture. Use a separate Compose project so its saved assignments and model configuration stay separate from live runs.

```bash
export COMPOSE_PROJECT_NAME=work-agent-demo
docker compose -f compose.yaml -f tests/compose.fixture.yaml up --build -d
docker compose -f compose.yaml -f tests/compose.fixture.yaml up -d --force-recreate app
```

Submit the two-shipment request and open both saved files before running the recreation command. After recreation, reopen them and submit the same request with one shipment. The fixture supports those two prompts and computes its outputs from the uploaded rows and arithmetic tool results.
