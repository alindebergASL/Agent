# A project brief from GitHub

Ask Work Agent about progress on `alindebergASL/Agent`, then return to the same assignment to see what changed. A read-only MCP service fetches public pull requests, review submissions, and PR conversation comments from GitHub. The assignment can preserve the records supporting each brief, even if a comment is later edited.

## Start the demo

From the repository root, use the development OpenAI key configured for the [app](../../README.md). The GitHub service uses anonymous public API reads. GitHub can throttle those reads, particularly on a shared IP. The app and GitHub service share a Compose network. Only the app publishes a port.

```bash
export COMPOSE_PROJECT_NAME=agent-github-status
export WORK_AGENT_PORT=8015
export OPENAI_MODEL=gpt-6.2-sol
docker compose -f compose.yaml -f examples/github-status/compose.yaml up --build -d
```

Open `http://127.0.0.1:8015`, forwarding port 8015 through SSH when using EC2. This Compose project has its own data volume. To use a different public repo, set `WORK_AGENT_GITHUB_REPOSITORY=owner/repo` in the environment before starting the demo and use its name in the assignment.

## Save the first brief

Create a new assignment without uploads. For example:

> How are we doing on alindebergASL/Agent, and what's next? My GitHub login is alindebergASL. Use the connected GitHub source. Save a short project-brief.md covering recent merges, reported live verification and its tested commit, decisions awaiting me, and your recommended next step. Record when you started checking in UTC, the period covered, and the PR numbers and head commits for open work. Link the PR and the specific comment or review supporting each important claim, along with its captured evidence. Say which verification or deployment facts are unknown.

Open the saved Markdown. GitHub’s merge event identifies the commit that landed. A verification comment identifies what its author tested and may name a different commit. Compare the two source records when interpreting the brief. On this repo, [PR #11](https://github.com/alindebergASL/Agent/pull/11) and its [EC2 verification comment](https://github.com/alindebergASL/Agent/pull/11#issuecomment-7150910374) provide an example.

## Ask what changed

Send a follow-up on **the same assignment** after new GitHub activity, or send it immediately to check whether anything changed during the interval:

> What changed since the last brief? Read project-brief.md. Check PRs updated since its recorded UTC start time. Reopen previously open PRs and any PR the brief flagged for a decision or missing verification, even if it already merged. Check their comments and reviews. Compare head commits, merge state and reported verification. Update project-brief.md with current status and a “Since the last brief” section. Give the comparison window and cite the captures and GitHub records for each change. If nothing material changed in the checked scope, say so.

Reopen the brief and select its earlier revision. For a reported change, compare its GitHub URL and the new captured evidence with the earlier brief. For an unchanged interval, check that the answer identifies the period and scope it actually checked. The assignment's **Run history (JSON)** shows which tools supplied the records for each turn.

Stop the demo from the same shell to keep its named volume for later:

```bash
docker compose -f compose.yaml -f examples/github-status/compose.yaml down
```
