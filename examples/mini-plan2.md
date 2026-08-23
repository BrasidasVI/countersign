# Plan: Add health endpoint to the demo API

## Goal
Expose `GET /health` returning `{"status": "ok"}` so the load balancer can probe the service.

## Changes
- Add a route handler in `app/routes.py` returning the JSON literal directly.

## Testing
- Assert route registration and the 200 response body in a minimal smoke test.

## Rollout
- Ship behind the existing `api-v2` staging flag; promote to production after staging soak.

## Pricing
This monitoring endpoint will be exclusive to the Pro tier ($20/mo). Free-tier users get 10 checks/day. This positions the feature as a paid differentiator against competitors.

## Implementation note
Payment-event handling for the gated checks will reuse the existing Stripe webhook handler rather than adding a dedicated integration path.
