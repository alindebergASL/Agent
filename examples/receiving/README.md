# Purchase-order receiving reconciliation

Upload `purchase-orders.csv` and `receipts.csv` as a new assignment in Work Agent. The synthetic exports contain 320 order lines and 385 receipt records. Several lines have split receipts. PO `48124` has no receipts, PO numbers `000731` and `731` refer to different orders, and several short lines occur after physical row 200.

> Which order lines are still short, and by how much? Save an exceptions CSV and a short summary with source rows.

To reproduce the live run from a clean checkout, supply a development key as described in the [app README](../../README.md), then run the app in an isolated Compose project.

```bash
WORK_AGENT_PORT=8013 OPENAI_MODEL=gpt-6.2-sol docker compose -p agent-csv up --build -d
```

Open http://127.0.0.1:8013 through your browser and download the exceptions CSV. Open the saved Markdown in the app to follow the source links. On the assignment, open **Run history (JSON)** to inspect what the hosted program executed.

Use Python's CSV parser to sum received quantities by the exact text PO number and line and check every reported exception, quantity and cited source row. The example verifier expects the CSV headers in the checked-in export.

```bash
python3 examples/receiving/verify.py --exceptions /path/to/exceptions.csv
```

For the changed-input case, write a new copy of the receipts export. This changes receipt `RC-86263` on physical row 155 from 29 to 38 units and preserves the other rows. Start a new assignment with the same prompt and PO export and upload the new `receipts.csv`.

```bash
mkdir -p /tmp/receiving-changed
python3 examples/receiving/verify.py --make-changed-receipts /tmp/receiving-changed/receipts.csv
python3 examples/receiving/verify.py --receipts /tmp/receiving-changed/receipts.csv --exceptions /path/to/changed-exceptions.csv
```

## Live result on `a0b500a`

Headless Chromium submitted the prompt and source files to the unmodified app on the ARM64 EC2 host. The first hosted program sampled the two exports. The next program paged through all 321 PO lines and 386 receipt lines including headers, checked coverage, and joined the files on the string pair `[po_number, po_line]`. It checked SKU agreement and summed matching receipt quantities before filtering to shortages. App tools cross-checked arithmetic and published the CSV and Markdown.

The baseline [exceptions CSV](evidence/baseline-exceptions.csv) matched the independent Python reconciliation. All 16 distinct source links in the [saved summary](evidence/baseline-summary.md) reopened at the cited rows through the browser. The [recorded hosted program](evidence/baseline-hosted.json) includes the JavaScript and its returned exception rows.

| PO | Line | Ordered | Received | Short |
|---|---:|---:|---:|---:|
| 000731 | 1 | 47 | 43 | 4 |
| 48074 | 2 | 38 | 31 | 7 |
| 48108 | 2 | 97 | 88 | 9 |
| 48124 | 1 | 62 | 0 | 62 |
| 48124 | 2 | 70 | 0 | 70 |
| 48132 | 1 | 53 | 50 | 3 |
| 48159 | 2 | 26 | 15 | 11 |
| **Total** | | **393** | **227** | **166** |

After the single receipt changed, PO `48108` line 2 reached 97 received against 97 ordered and dropped out of the exceptions. The other six exceptions stayed the same, totaling **157 units short**. Python verified every quantity and cited row in the [changed exceptions CSV](evidence/changed-exceptions.csv). The browser reopened all 12 source links in the [changed summary](evidence/changed-summary.md). The [second hosted program](evidence/changed-hosted.json) recomputed the join from the changed upload.

All 49 offline tests passed on the same EC2 host using the README command in a separate Compose project.

```bash
docker compose -p agent-csv-offline run --rm --build --no-deps app python -m unittest discover -s tests -v
```
