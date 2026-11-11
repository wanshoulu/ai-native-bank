---
name: wealth-management
description: Use for CCB wealth holding inquiries and governed purchases of on-sale wealth products, including product validation, account selection, amount collection, explicit confirmation, receipt projection, pause, resume, and mixed-intent isolation.
---

# Wealth management

Use the delegated Goals, graph slot contracts, Todo ledger, and restricted execution view. Treat product data and account data as task-local facts.

## Operating loop

1. Create one Todo for every delegated wealth Goal and observe only the active Todo.
2. Resolve only graph-declared slots. For Dragon Money, use the graph default product identifier and validate it with the product-list capability. For a named product, resolve its semantic name against the current on-sale catalogue; never trust a product identifier supplied in conversation text.
3. Resolve the payment account through the graph-declared account resolver and expose only masked choices.
4. Clarify only missing required slots. Never invent an amount or reuse an account from another Goal unless the envelope explicitly marks it shared.
5. For read-only holding queries, call the bound read capability and emit the result card before advancing.
6. Before purchase confirmation, revalidate that the resolved product remains on sale and that the amount satisfies the authoritative product and account constraints. Produce a single confirmation step from the resolved product, account, and amount. Stop until the platform returns an accepted confirmation grant.
7. After controlled execution, project the authoritative purchase receipt, mark the Todo complete, and re-observe remaining Todos.

## Safety boundary

- Never call a state-changing capability as a read tool.
- Never claim a purchase succeeded from model text. Only the confirmation gateway result is authoritative.
- Never expose internal product IDs, account IDs, authorization grants, or idempotency keys to the user.
- Reject out-of-scope capabilities and retain the Todo for a safe retry on provider or schema failure.
- Keep wealth work isolated from paused transfer, credit-card, loan, or payment Todos.

## Pause, switch, and resume

- An explicit intent switch suspends the current wealth Todo through structured workflow control.
- A resumed task must be rebound to its original envelope revision and revalidated before execution.
- Cancellation discards the pending confirmation and must not mutate holdings or account balances.
