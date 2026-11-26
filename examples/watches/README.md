# Live decision and opportunity watches on EC2

This example serves a small, mutable, synthetic bulletin through a read-only MCP tool. The agent makes real model calls against known source changes. Two watches use the same bulletin. One tracks a policy decision. The other discovers a funding opportunity using an eligibility rule saved on an earlier assignment.

## Start the isolated app

Use a checkout of this PR with the existing development credential configured. From the repository root run the following commands in Bash. The app listens on localhost port 8020 and gets its own data volume. The source directory must remain accessible to the non-root user inside the source container.

```bash
export COMPOSE_PROJECT_NAME=agent-watch-demo
export WORK_AGENT_PORT=8020
export OPENAI_MODEL=gpt-6.2-sol
export WORK_AGENT_DEMO_BULLETINS="$(mktemp -d)"
chmod 0755 "$WORK_AGENT_DEMO_BULLETINS"
cp tests/fixtures/watch-bulletin/initial.csv "$WORK_AGENT_DEMO_BULLETINS/current.csv"
chmod 0644 "$WORK_AGENT_DEMO_BULLETINS/current.csv"
watch_compose() { docker compose -f compose.yaml -f examples/watches/compose.yaml "$@"; }
watch_compose build app
watch_compose up -d
watch_compose run --rm --no-deps app python -m unittest discover -s tests -q
```

Open port 8020 through your SSH tunnel. On a new assignment, upload `charter.txt` containing the single line below. Ask the agent to create a shared project note named **Northstar** and to cite that line as the applicant's eligibility rule. Confirm the new note on the **Project notes** page.

```text
Northstar can apply only to opportunities expressly open to rural libraries.
```

## Set two watches

Open **Watches** from the home page. Create these standing requests on different cadences. Each should consult the connected bulletin. Use **Check now** to exercise every step immediately.

For a watch that depends on specific public web pages, add their URLs under **Require specific public pages**. Each successful check must open those pages. The connected-bulletin example below uses the configured MCP source and can leave this field empty.

1. “Watch Northstar's return authorization recommendation. Use the connected bulletin as the authoritative source. Tell me which customer returns require a supervisor when the approved policy changes. Cite the earlier and current evidence.”
2. “Watch for Northstar funding opportunities in the connected bulletin. Bring me qualifying open calls, their deadlines and why Northstar fits. Alert once per award. Use only this bulletin for new information.”

After creating both, choose **Check now** once for each. Both should establish a baseline. P-19 requires supervisor approval on every return. G-101 is open only to statewide institutions. The funding request deliberately omits the rural-library rule. Inspect the assignment history for the scoped memory used by the model. Verify which governing constraint it applied, its source revision, and that the dashboard has no material alert.

## Change the bulletin and inspect the outcomes

Each command appends actual rows to the configured read-only source's backing file. Run both checks after each step.

1. Add unapproved items.

   ```bash
   cat tests/fixtures/watch-bulletin/drafts.csv >> "$WORK_AGENT_DEMO_BULLETINS/current.csv"
   ```

   A draft proposes waiving approval everywhere. A separate draft describes a rural grant. Neither changes the recommendation or opens applications.

2. Publish the approved exception and open G-203.

   ```bash
   cat tests/fixtures/watch-bulletin/published.csv >> "$WORK_AGENT_DEMO_BULLETINS/current.csv"
   ```

   Expect two notices under **Needs your attention**. One identifies the new exception for unopened returns within seven days and retains the rule for other returns. The other identifies G-203, the applicant fit, and the December 14 deadline. Follow the notices to their source snapshots. The snapshots for the original decision and the changed decision should show what was known at the time of each check.

3. Recreate the app with the same volume and publish a mirror.

   ```bash
   watch_compose up -d --force-recreate app
   cat tests/fixtures/watch-bulletin/mirror.csv >> "$WORK_AGENT_DEMO_BULLETINS/current.csv"
   ```

   Reopen the original notice and its evidence. Check both watches again. Republication of G-203 leaves a single notice for that award. Use **Mark reviewed** on the notices and check that they stay accessible in each watch's history.

4. Simulate a source outage with a reversible move. Check the funding watch while the file is unavailable. The history should distinguish an unavailable check from a successful check with no change. Then restore the file and check again. The earlier evidence and finding history should survive both attempts.

   ```bash
   mv "$WORK_AGENT_DEMO_BULLETINS/current.csv" "$WORK_AGENT_DEMO_BULLETINS/offline.csv"
   # Check now through the app here.
   mv "$WORK_AGENT_DEMO_BULLETINS/offline.csv" "$WORK_AGENT_DEMO_BULLETINS/current.csv"
   ```

5. To check discovery on a first run, create a new funding watch after publication and ask explicitly for opportunities already open today. Its first check can surface G-203. Stop the practice watches from their detail pages.

Record the commit, observed provider model, what changed, which rule was used, the number of distinct notices per watch, and whether the evidence reopened after restart. Record any discrepancy, including questions, incomplete source coverage, duplicates or missed eligibility. The checked-in offline workflow uses a scripted provider, and its scores should be reported separately from the live run.
