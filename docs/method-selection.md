# Selecting a reusable procedure

Every assignment and follow-up searches the saved method titles and scopes with SQLite FTS5. English stemming matches inflections, and BM25 gives scope terms more weight than title terms. Matches for the current request come first. The worker also searches the first and latest prior user requests and upload filename stems so a short follow-up can recover the original scope. Selection remains sensitive to the words used in a scope.

Each turn receives a ranked page of short summaries. A page holds about 8 KiB of serialized metadata, and `list_more_methods` can continue through all matching summaries. Opening a procedure with `read_method` records the retrieval in run history. The evidence and source assignment stay accessible from the method page. The search index backfills existing methods and tracks edits, agent publications and removals transactionally.

## Paired decision evaluation

The experiment compares the existing recent-eight policy and scope-based selection on the same requests, source bytes, method corpus, model instructions, tools and application worker. Only the method selector changes. Each arm uses a temporary database. Trials alternate policy order. The target purchasing and release procedures are both older than the eight newest entries. A recent ocean freight method provides a plausible but out-of-scope match.

| Assignment | Expected action from the source |
| --- | --- |
| Purchase with two shipments | North costs $1,060. South costs $1,050 and wins. |
| Same quotes, one shipment | North costs $1,030 and wins. South costs $1,050. |
| Revised North freight, two shipments | North costs $1,024 and wins. South costs $1,050. |
| Schema migration with a failed restore drill and two approvals | Hold the release and repeat the drill. |
| Same migration after a passing restore drill | Ship the release. |

The scorer separately checks that the cited rows were read, the exact citations, totals or required action, winner or release decision, and successful retrieval of the scoped procedure. Each JSON record includes the actual output, offered and retrieved method IDs, observed provider model and token usage when available. A model can reach the right decision directly from the sources. Score its decision even when it skips the method.

From the branch checkout on EC2, use the existing app development key in the Compose environment. These runs use temporary directories inside one-off containers.

```bash
docker compose -p agent-knowledge-eval run --rm --build --no-deps app python -m tests.method_policy_eval --live --repeats 2 > method-policy-live.json
```

The live run uses `gpt-6.2-sol` at the official Responses endpoint. Review both policies' paired decisions, citations, observed model names, and token usage before tuning the number of methods to show. This small corpus tests selection and application on two domains. Larger or differently curated collections need their own measurements.

To check the experiment wiring without an API credential, run the fixture.

```bash
docker compose -p agent-knowledge-eval run --rm --build --no-deps app python -m tests.method_policy_eval --fixture --repeats 2 > method-policy-fixture.json
```
