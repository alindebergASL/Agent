# Selecting a reusable procedure

Every assignment and follow-up searches the saved method titles and scopes with SQLite FTS5. English stemming matches inflections, and BM25 gives scope terms more weight than title terms. Matches for the current request come first. The worker also searches the first and latest prior user requests and upload filename stems so a short follow-up can recover the original scope. Selection remains sensitive to the words used in a scope.

Each turn initially receives up to three ranked method summaries within an approximately 8 KiB metadata budget. If more methods match, `list_more_methods` starts at the supplied offset and returns further byte-limited pages until all matches have been offered. Opening a procedure with `read_method` records the retrieval in run history. The three-summary limit applies only to reusable procedures. Project claims and their evidence follow the separate project-memory selection described below.

The evidence and source assignment stay accessible from the method page. The search index backfills existing methods and tracks edits, agent publications and removals transactionally.

## Paired decision evaluation

The historical experiment compares the recent-eight policy and scope-based selection on the same requests, source bytes, method corpus, model instructions, tools and application worker. Both arms retain their original byte-only pagination so only the selector changes. Each arm uses a temporary database. Trials alternate policy order. The target purchasing and release procedures are both older than the eight newest entries. A recent ocean freight method provides a plausible but out-of-scope match.

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

## How many method summaries should arrive before the first action?

The four arms show one ranked summary, the production preview of three, eight ranked summaries, or the former byte-only preview. All four see the same five scenarios, source files, model instructions and ranking. The catalog includes additional branch-specific procedures so each size exposes a different number of summaries. The model can page through the remaining matches in every arm. The report records initial IDs, further pages read, procedure reads, grounded decisions and tokens.

On the initial live EC2 run, all four arms produced 10/10 correct, cited decisions. Showing three methods surfaced the target immediately in all ten trials, required zero extra pages and used 505,873 input tokens. The former byte-only page exposed 21 summaries on this catalog, required three additional page reads and used 639,619 input tokens. These results motivated the three-summary default for this model. In reports from PR #17 the columns labeled `ranked-3` and `production` correspond to today's `production` and `byte-budget-only`, respectively. [Original live verification](https://github.com/alindebergASL/Agent/pull/17#issuecomment-7180686943)

```bash
docker compose -p agent-knowledge-eval run --rm --build --no-deps app python -m tests.method_policy_eval --live --compare-prefixes --repeats 2 > method-prefix-live.json
```

With two repeats this runs 40 assignments. Substitute `--fixture` to check the experimental wiring offline. Run without `--compare-prefixes` to use the original corpus and recent-eight comparison.

## Does surfacing a project claim change the decision?

A separate paired evaluation sends a fresh assignment to the ordinary worker with no upload and no instruction to recall a project note. Each request asks for a receiving slip containing a desk code and the exact supporting source line. The two archives use different desks. One archive later authorizes an exception for cobalt-sealed crates. Four cases cover the original rule, the same request after the amendment, a crate outside the exception, and a similar facility with a different rule.

For each pair, the two arms start with copies of the same seeded volume, source URLs and note revisions. The two arms receive identical system instructions and have access to the same tools. The on-demand arm receives an empty memory preview and can search notes or reopen evidence. The proactive arm receives the populated pre-action preview. Order alternates across cases and repeats. The scorer measures whether the controlling claim appeared in the initial context, whether the agent actually read the source line, whether its desk code was correct, and whether the citation resolves to that exact original line. The report includes the slip, observed provider model and token usage. A model that finds a note but selects the wrong desk fails the action check.

From the PR checkout on EC2, run the paired live comparison with the existing app development credential. The one-off container uses temporary evaluation data.

```bash
docker compose -p agent-watch-memory-eval run --rm --build --no-deps app python -m tests.project_memory_eval --live --repeats 2 > project-memory-live.json
```

For a local wiring check, replace `--live` with `--fixture`. There are 16 assignments with two repeats. The four cases exercise the use of known, named project scopes and exact source history. Measuring the best preview size or success on unfamiliar projects requires separate trials. Report action correctness and citation accuracy separately, even if both arms perform equally well.
