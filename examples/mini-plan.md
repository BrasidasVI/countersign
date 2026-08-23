# Plan: Add health endpoint to the demo API

## Goal
Expose `GET /health` returning `{"status": "ok"}` so the load balancer can probe the service.

## Changes
- Add a route handler in `app/routes.py` returning the JSON literal directly.

## Testing
No new tests needed; the endpoint is trivial.

## Rollout
Deploy directly to production after merge.
