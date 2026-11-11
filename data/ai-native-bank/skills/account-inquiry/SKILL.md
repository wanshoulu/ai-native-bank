---
name: account-inquiry
description: Use for CCB account balance, transaction-history, and account-detail inquiries, including account selection, multiple accounts, combined balance-and-history requests, pagination, pause, resume, and permission-safe result projection.
---

# Account inquiry

Use the delegated goals, graph slot contracts, and restricted execution view. This Skill is read-only: never plan a state-changing capability. Balance, transaction-history, and account-detail queries (such as card type, status, or masked number) all belong to this Skill.

## Operating loop

1. Create one Todo per delegated account inquiry goal or requested account instance.
2. Observe the active Todo and resolve its semantic account reference with the graph-declared account resolver.
3. Clarify only when the required account reference remains unresolved. Present masked account choices.
4. Call only the query capability bound to that intent.
5. Validate the returned schema, project a user-safe card, mark the Todo complete, and re-observe remaining Todos.

When the platform returns `authoritative_task_completion` after a resolver turn, treat it as the immutable success fact for only the referenced active Todo. Mark that Todo complete, then re-observe the ledger and select the next Goal. Do not call the completed capability again and do not carry its account reference into the next Todo.

During a clarification-resume resolver phase, use only the resolver capability visible in the execution view. Do not attempt the query capability in that turn; the platform resolves the reference, invokes the task-bound query safely, and returns `authoritative_task_completion` for the next loop observation.

## Task shapes

- Preserve balance and transaction-history requests as distinct Todos even when they refer to the same account.
- Multiple accounts may be independent tasks. Parallel planning is allowed only when the task policy and execution view both allow it.
- Shared account references can be reused only when explicitly shared in the envelope.
- Pagination is a continuation of the same Todo, not a new intent.

## Mixed multi-intent participation

- Treat every delegated account inquiry as a task-local Todo, even when another Skill owns adjacent Todos.
- Observe only the active Todo. Resolve its account reference through the graph-declared resolver and emit masked choices when ambiguous.
- After a read succeeds, emit the completed result before observing the next Todo.
- Never propagate an account reference to another Todo unless the envelope explicitly declares a graph-approved shared binding.

## Safety and recovery

- Never ask for or reveal a full account number.
- Internal account identifiers may be used only inside the authorized tool boundary and never copied to model-visible state.
- On provider unavailability or schema drift, fail closed and retain the semantic Todo for retry.
- Pause, resume, and cancel only through structured workflow control and the referenced revision.
- Do not convert an inquiry into transfer, payment, or account-management work without a new delegated intent.
