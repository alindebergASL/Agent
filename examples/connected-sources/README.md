# Reconcile two connected sources

Use two MCP servers as the purchasing and warehouse systems. They serve the same 320 order lines and 385 receipts as the [upload example](../receiving/), returning individual source records and a cursor for each next page. The agent writes the join. Each service sees only its own read-only CSV mount. The app reaches both services over the Compose network.

## Run with a live model

From the repository root, set your development API key as in the [app README](../../README.md). Start the demo in the same shell.

```bash
export COMPOSE_PROJECT_NAME=agent-connected-demo
export WORK_AGENT_PORT=8014
export OPENAI_MODEL=gpt-6.2-sol
docker compose -f compose.yaml -f examples/connected-sources/compose.yaml up --build -d
```

Open `http://127.0.0.1:8014`. When using EC2, forward that port through SSH. Start an assignment with the following request and no uploads:

> Use purchasing and warehouse to find every purchase-order line that is still short. Sum split deliveries and preserve the exact PO and line identifiers. Save an exceptions CSV with columns po_number, po_line, ordered_qty, received_qty, short_qty, po_source_row, and receipt_source_rows. Use the source_ref strings returned by each system in the source columns. Save a short Markdown summary with links to the source evidence for each exception. For a missing receipt, explain how you checked all pages.

Open the CSV and Markdown. Follow an evidence link from the Markdown to see the page of source records captured during the assignment. Use the cited PO, line and receipt identifiers to locate the supporting records. Open **Run history (JSON)** to check which calls share a hosted program ID and whether the program fetched every page. The sample servers return at most 80 records per page, making four pages for purchasing and five for warehouse.

Download the CSV and use the independent verifier with its actual download path. The expected result is seven short lines totaling 166 units. PO `000731` line 1 is four short. PO `731` line 1 was filled. PO `48124` has no receipts.

```bash
python3 examples/receiving/verify.py --exceptions /path/to/exceptions.csv
```

## Change one upstream receipt

After the first assignment completes, generate a changed source in a fresh directory. In the same shell, point only the warehouse fixture at that copy. The original tracked CSV stays available for another baseline run.

```bash
changed_dir=$(mktemp -d)
python3 examples/receiving/verify.py --make-changed-receipts "$changed_dir/receipts.csv"
export WORK_AGENT_DEMO_RECEIPTS="$changed_dir/receipts.csv"
docker compose -f compose.yaml -f examples/connected-sources/compose.yaml up -d --no-deps --force-recreate warehouse
```

Start a fresh assignment with the same request. Receipt `RC-86263` now has 38 units instead of 29, filling PO `48108` line 2. The expected result is six short lines totaling 157 units. Check the new export against the changed source. Reopen the evidence from the first assignment to verify it still shows 29 units for that receipt.

```bash
python3 examples/receiving/verify.py --receipts "$WORK_AGENT_DEMO_RECEIPTS" --exceptions /path/to/changed-exceptions.csv
```

These fixtures read their CSV on each page request. Change the fixture between finished assignments so each reconciliation sees one consistent dataset. To stop the demonstration and keep its saved assignments, use the following command.

```bash
docker compose -f compose.yaml -f examples/connected-sources/compose.yaml down
```
