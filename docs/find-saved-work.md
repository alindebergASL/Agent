# Find a file from earlier work

Start a fresh assignment with a request such as “Find my last University of Iowa meeting brief and check what has changed.” Work Agent searches the full text of original uploads and the latest published revision of each generated file in its single-user workspace. A filename, a phrase from the assignment, an identifier, or a phrase from the middle of a document can locate a file.

A matching passage includes its physical source lines and an excerpt. CSV records retain their column headings and quoted multiline ranges. Markdown passages retain their section headings. The agent opens a candidate and reads the numbered original lines before citing it.

The SQLite index combines exact filename and keyword matches with contextual similarity. For example, two documents that use *shipping* and *freight* can match when both terms occur alongside similar language. The model may also supply up to three paraphrases in one search. An exact identifier such as `000731` is searchable with its leading zeros. Indexing and ranking run locally on the app's data volume. Selected passages are returned to the assignment's model. Existing volumes are indexed at startup, and a new publication or edit is indexed for the next search.

The agent can narrow a search or ask which assignment you meant. Opening a result pins the revision that appeared in the search. You can also choose **Start an assignment with this revision** from a current or historical file. Follow the new report's citation back to the saved revision. Citations to CSV lines can take you directly to the cited row.

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

## Reuse two original uploads

To try original quotes in a separate instance, use `COMPOSE_PROJECT_NAME=agent-original-uploads` and `WORK_AGENT_PORT=8018` with the Compose commands above. In one assignment, upload both CSVs from `examples/quotes/`. Then start a new assignment with this request and leave the upload field empty.

> Find the two original supplier quotes I uploaded earlier. Use their own prices and freight terms to compare the delivered cost of 20 units in two shipments. Also show me the one-shipment case. Save the comparison with links to the original quote rows.

For two shipments, expect Supplier A at $2,060 and Supplier B at $2,080. For one shipment, expect $2,030 and $2,080. Follow each row citation to line 2 of the earlier assignment and verify that **Download original** returns the bytes you supplied. Recreate the isolated app with the same volume and repeat the link and download checks.

## Design references

SQLite FTS5 provides keyword search and weighted BM25 ranking inside the application's existing database. An exact filename ranks first. The model gets short descriptions to distinguish candidates, then reads the selected file on demand. This keeps the source revision available for verification and lets us evaluate actual choices on similar filenames. [SQLite FTS5](https://www.sqlite.org/fts5.html#the_bm25_function) · [Anthropic on tool results and evaluation](https://www.anthropic.com/research/writing-tools-for-agents) · [Anthropic on on-demand retrieval](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

## Follow a citation back to the decision

Open an original upload or a saved revision and choose **Where this original was used** or **Where this revision was used**. Each citing report link opens the published revision that actually links to the source. The ledger also shows the cited rows, claims in project notes, scoped source changes, and assignments that selected or read the file. A successful nonempty tool read is recorded separately from a pinned file and from a citation. A filename or a Markdown link inside a code sample does not establish a relationship.

An indirect review candidate follows two explicit citations. For example, a rollout decision cites revision 1 of a checklist, and that checklist cites a row of an approval policy. Review the checklist and decision to determine whether a changed policy affects the outcome. A decision citing revision 2 of the checklist appears on the policy ledger only if that revision also links to the policy. The links preserve historical revisions, and tracing leaves the decision unchanged.

The agent can use `find_evidence_uses` with a listed original upload, an exact saved revision in the current assignment, or a previously selected file’s reference ID. It can open either report in a returned path and continue tracing from that revision. Results are paged. Inspect the revision and the cited evidence before changing a conclusion.
