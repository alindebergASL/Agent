# Work Agent

Read docs/blueprint.md for the product direction. Use the current task to decide what to build in this change. Keep one application-owned model and tool loop. Treat examples under examples/ as inputs a user can upload. Implement general tools that can handle other files and questions.

Verify changes through the user-facing path. Report what ran with a live model, what ran with a fixture, and what remains unverified. Commit instructions for starting and testing the app from a clean checkout as soon as those commands exist.

Use Docker Compose as the normal entry point for local development, tests, and the first EC2 deployment. Package the app and runner together. Store durable state in the mounted data directory. Document the exact container commands and report where they ran.
