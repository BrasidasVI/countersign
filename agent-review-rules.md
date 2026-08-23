# Review invariants — Ladderly
# Used with: python orchestrator.py "..." --review-rules .agent-review-rules.md
# These ADD to the built-in defaults (test coverage, rollout safety, repo verification).
# Use --replace-default-rules to make this file the complete rule set instead.

- Stripe Tax: changes to payment or subscription flows must state whether Stripe Tax
  configuration is affected and in which jurisdictions.
- Auth coverage: changes to authentication or payment flows must identify which
  existing tests cover them, or explicitly note that new tests are required.
- Frontend types: API route changes must include corresponding frontend TypeScript
  interface updates if applicable.
- Dependencies: new dependencies require a brief justification of why existing
  packages are insufficient.
- Pricing and tier gating: any feature that is gated by subscription tier must
  explicitly state the tier boundary and rationale; this is a product decision
  requiring human sign-off via open_questions.
