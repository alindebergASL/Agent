# Can the agent change a meeting recommendation for the right reason?

This exercise uses publicly available University of Iowa guidance and a fictional meeting. A sponsor changes one proposed input. The agent should identify which part of the work can still proceed and leave the earlier recommendation available with its original evidence.

## Run it through Work Agent

Start from the repository root with the app’s development credential available in the shell. Use an isolated project and data volume so another Iowa assignment cannot supply the answer.

```bash
export COMPOSE_PROJECT_NAME=agent-iowa-meeting
export WORK_AGENT_PORT=8027
export OPENAI_MODEL=gpt-6.2-sol
docker compose up --build -d
```

1. In the web app, create an assignment with the [previously generated November 23 brief](../../docs/increment-1/brief-final-university-of-iowa-responsible-ai-meeting-brief.md) as its only upload. Ask “Archive this University of Iowa meeting brief for a future assignment.” Wait for completion.
2. In a **fresh assignment**, upload only [meeting-scope.txt](meeting-scope.txt). Ask “Prepare a one-page University of Iowa meeting brief for the attached agenda. Find our earlier Iowa brief, check current official university sources, and recommend what to do in the meeting. Include questions that would help us reach a decision. Save meeting-brief.md with citations.” Download the result and inspect its links. Keep this assignment open.
3. Attach only [scope-amendment.txt](scope-amendment.txt) as a follow-up. Ask “The sponsor sent this update. Reassess the recommendation, explain what changed and why, and update the same brief. Keep the earlier rationale accessible.” Save the revised download. Give the model neither reference answer.
4. Recreate the isolated app container with `docker compose up -d --force-recreate app`. Reopen revision 1 and revision 2 of the saved brief. Compare each download to its own pre-restart copy. Verify that the current brief opens revision 2, the historical link opens revision 1, and the local evidence links identify the intended uploads and lines.

## What to inspect

| Question | Evidence of success |
| --- | --- |
| Did the agent use prior work? | A saved-work search, selection and read identify the original November 23 brief. At least one useful idea carries forward with a link. |
| Did it research the current situation? | Open the official pages in its citations. Match the decision-critical claims to the pages and verify the run history shows fresh research. A search result or previously written brief alone cannot verify a current rule. |
| Does the first recommendation fit the facts? | The public-source FAQ can proceed in the scenario’s licensed workspace. The research support lead owns verification. |
| What changed after the amendment? | The brief keeps the public FAQ viable and excludes identifiable patient records from ChatGPT Edu. It attributes the change to the sponsor’s input. It preserves the audience, meeting purpose, and review commitment. |
| Would a reader act correctly? | The brief offers a usable next step for each part. If it mentions the IHDR Data Enclave, it describes a route to investigate and avoids implying approval for this workflow or this team. |
| Does its history still make sense? | Citations to the original scope, amendment, public guidance and earlier recommendation remain distinguishable. Both versions reopen. |

Record the model, commit, first-attempt outcome of each assignment, any retries, and which checks passed. If university guidance has changed since November 27, 2026, assess the result against the current official guidance and record the difference.

## Reference answers

The [original](reference-original.md) and [revised](reference-revised.md) briefs illustrate two usable decisions. They are for the evaluator after the run. The exercise accepts different wording, citations and meeting questions when the evidence supports them.
