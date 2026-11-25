# Find a file from earlier work

Start a fresh assignment with a request such as “Find my last University of Iowa meeting brief and check what has changed.” Work Agent searches published filenames, original requests, and the request that first produced each file. It returns up to eight results with a filename, request excerpt, date, and revision. A match on text that appears only inside a document requires another clue such as the filename. Search covers the app's single-user workspace.

The agent can narrow its search, ask which assignment you meant, or open a result and read numbered lines. Opening a result pins the revision that appeared in the search. You can also choose **Start an assignment with this revision** from a current or historical file. Follow the new report's citation back to the saved revision. Citations to CSV lines can take you directly to the cited row.

## Try it on an isolated EC2 deployment

Use a shell with the existing development key configured. From a checkout of the PR, run:

```bash
export COMPOSE_PROJECT_NAME=agent-find-saved-work
export WORK_AGENT_PORT=8017
export OPENAI_MODEL=gpt-6.2-sol
docker compose up --build -d
docker compose run --rm --no-deps app python -m unittest discover -s tests -q
```

Open port 8017 through the SSH tunnel.

1. In separate assignments, create short practice briefs for the **University of Iowa** and **Iowa State University**, in that order. Ask each to save `meeting-brief.md`. Add a distinct user note to the Iowa brief, such as “Ask how the help desk handles access requests.”
2. In a fresh assignment with no uploads, ask “I'm meeting the University of Iowa again. Find my last brief, check what has changed using official sources, and save three questions worth asking. Cite the earlier brief and the fresh sources.” Verify which institution and revision it selected. Check the dates and claims against the official pages. A change requires evidence of what was true before and what is true now. The agent can also report that it found no verified change.
3. In a separate assignment, save `purchases.csv` with columns for item, price, and quantity sold. Use bananas at $1 × 10, apples at $1.50 × 20, and kiwis at $2 × 5. Start a new assignment: “Find my purchases CSV. What would revenue be if apples sold for $2? Save the calculation with links to the source rows.” Verify $50 originally, $60 in the scenario, and that the citation opens the original CSV revision at the cited line.
4. Edit the original Iowa Markdown after the updated brief is saved. Run `docker compose up -d --force-recreate app` and reopen the citation. Verify that it still shows the earlier bytes and user note. Check **Run history (JSON)** for the searches, selected revision, reads, and evidence of fresh research.

## Design references

SQLite FTS5 provides keyword search and weighted BM25 ranking inside the application's existing database. An exact filename ranks first. The model gets short descriptions to distinguish candidates, then reads the selected file on demand. This keeps the source revision available for verification and lets us evaluate actual choices on similar filenames. [SQLite FTS5](https://www.sqlite.org/fts5.html#the_bm25_function) · [Anthropic on tool results and evaluation](https://www.anthropic.com/research/writing-tools-for-agents) · [Anthropic on on-demand retrieval](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
