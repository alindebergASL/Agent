# Knowledge across assignments

Work Agent can revisit a decision when its supporting evidence changes. A decision can be a recommendation, a policy interpretation, a project assumption, or a calculated result. The same assignment loop and tools handle each.

Suppose a handbook requires manager approval at two labs. An approved addendum exempts escorted visitors at one lab. A draft proposes dropping approval everywhere. The agent can revise the affected lab's instructions, explain why the other lab still requires approval, and record the unresolved conflict with the draft. A reader can open the original recommendation, see the handbook line that justified it, and follow the amendment from there.

## What persists

| Workspace object | How it is used |
| --- | --- |
| Original upload or published artifact revision | Preserve its bytes and identity. Search across full supported text, including CSV rows and Markdown sections. Open a numbered passage before using it as evidence. |
| Evidence relationship | Link two exact source lines. Record whether one supersedes, qualifies or contradicts the other, where the relationship applies and why. A later timestamp on its own is insufficient evidence of replacement. |
| Decision or project note | Store claims with their source links. Each published revision can explain what changed and link a replacement claim to its predecessor. Historical revisions and user edits remain inspectable. |
| Citation index | Find report revisions or note claims that depended on a source. Follow a report that another decision cited. A file that was only selected or read has its own entry. A citation identifies work to reconsider. The agent must still determine whether the outcome changes. |
| Reusable method | Retrieve procedures by title and scope for the assignment. The agent can inspect the procedure and compare it against the current inputs. |

The Compose app and its durable data have separate lifetimes. Source bytes, published revisions, notes, relationship records and the derived SQLite indexes share the existing named data volume. Replacing the application container reuses that volume. Searching runs locally. Selected passages, claims, tool outputs, and the user's assignment enter the configured model's context. Search covers original text uploads and the latest published CSV or Markdown of each name and assignment. Earlier generated revisions stay directly readable through their citations and history.

## What the agent sees before acting

On ordinary assignments, follow-ups, and watch checks, the worker searches current project notes by scope and claim text. When the request names a scope, or the user starts a watch from a specific note, matching claims enter the first model request with their author, revision, exact source links, and any recorded changes to their evidence. The agent can immediately reopen a source passage. A recorded exception or contradiction identifies what needs rechecking before acting.

Potential matches based on claim text alone appear as scope suggestions. For instance, “write the lantern inventory” could refer to two different sites. The model can open the notes or ask which site the user means. A title or claim needs some matching vocabulary for automatic discovery. The model can also search using alternate wording. A user edit replaces the searchable claim text in the same transaction that creates its new revision. Older revisions remain available for historical questions.

The serialized note preview uses an 8 KiB budget. Full notes and sources remain readable through their usual tools. Run history records the exact memory preview shown at the start of the turn. Subsequent evidence reads and method retrievals have separate receipts. A historical claim can guide a watch's search. Reporting a new finding requires evidence from the current check.

## Try a change of evidence on EC2

From the PR checkout, use an isolated Compose project with the existing development key in its environment.

```bash
export COMPOSE_PROJECT_NAME=agent-knowledge-foundation
export WORK_AGENT_PORT=8019
export OPENAI_MODEL=gpt-6.2-sol
docker compose up --build -d
docker compose run --rm --no-deps app python -m unittest discover -s tests -q
```

1. Upload a text file named `handbook.txt` with the lines below. Ask “Create a North and East lab visitor access note, recommend what reception should do, and cite the policy.” Open the first note revision and the cited rule.

   ```text
   Visitor access handbook, in force.
   Both North and East labs require a manager signature for every visitor.
   Every visitor must complete orientation.
   ```

2. In a **fresh assignment**, attach two short files. In `approved.txt`, write “Approved effective today. At the North lab only, escorted visitors may enter without the manager signature. No other requirement changes.” In `proposal.txt`, write “Draft for discussion. Propose removing the signature requirement at all labs. This proposal has not been adopted.” Ask “Reassess our visitor instructions using these documents. Update the project note and tell me which existing decisions need review. Explain the grounds for both the current and earlier guidance.”
3. Verify the resulting actions for an escorted North visitor, an unescorted North visitor, and an East visitor. Check that orientation still applies and that the draft's status is clear. Open both note revisions and the exact evidence links. Then ask in a fresh assignment “Why did we previously require a signature in the North lab, and what changed?” A correct answer identifies the old rule and the narrow exception.
4. For arithmetic, change only a source freight rate and repeat a purchasing decision. With ten units, A at $100/unit plus $30/shipment and B at $103/unit plus $20/order, B wins by $10 for two shipments. With A's rate amended to $20/shipment, A wins by $10. A already won for one shipment, and its margin increases from $20 to $30. Inspect the arithmetic receipt for its named inputs, formula, source text and governing condition. A separate prose line can govern a numeric input.
5. Run `docker compose up -d --force-recreate app` in the same isolated project. Reopen both decision revisions and their cited source rows. [Run the paired method-selection experiment](method-selection.md).

Source relationships are explicit and scoped. The agent interprets the authority and meaning of each passage when it works on an assignment. A relationship prompts review. A new assignment or follow-up can publish a reasoned revision to a decision.

## Research behind the implementation

The three design questions are whether a retrieved constraint affects the action, whether a synthesized note can be traced back to its sources, and how much method context helps this model on actual work. The paired experiment measures method use separately from decision correctness. [RAG in a Trenchcoat](https://arxiv.org/abs/2611.22179) · [Can LLM Agents Organize Their Own Memory?](https://arxiv.org/abs/2611.11929) · [How Much Should an Agent Remember?](https://arxiv.org/abs/2611.26361).
