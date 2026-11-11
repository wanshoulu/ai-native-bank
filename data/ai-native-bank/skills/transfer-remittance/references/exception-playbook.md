# Transfer exception playbook

- Missing slot: clarify the next graph-declared required slot.
- Ambiguous amount scope: ask one bounded scope question before account selection or draft creation.
- Resolver no match: use a secure structured form; do not fabricate an identifier.
- Provider unavailable or schema mismatch: fail closed and retain resumable semantic state.
- Confirmation expired or summary changed: discard the stale checkpoint and rebuild from current state.
- Duplicate or retried invocation: rely on platform idempotency and never issue a second call merely because a response is delayed.
- User exits: pause through structured workflow control and return a resumable user-facing notice.
