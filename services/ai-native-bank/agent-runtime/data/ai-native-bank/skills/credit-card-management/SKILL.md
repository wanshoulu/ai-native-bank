---
name: credit-card-management
description: Use for CCB credit-card listing, bill inquiry, repayment, transaction inquiry, activation, loss reporting, limit change, and security-setting tasks, including multiple cards, required-slot clarification, confirmation, pause, resume, and receipts.
---

# Credit-card management

Use graph-declared goals, slots, capability bindings, task policy, and the restricted execution view. Do not infer card identifiers or mutation fields from display text.

## Operating loop

1. Create one Todo for every delegated goal and every requested card instance.
2. Resolve the semantic card reference through the card-list capability and retain only masked user-visible data in model context.
3. Read any additional current state required by the graph before planning an update.
4. Clarify required unresolved slots in graph order.
5. For queries, call the allowed read capability and complete the Todo after safe projection.
6. For state changes, prepare a confirmation checkpoint; do not call the user-action capability from the model loop.
7. Consume the returned immutable receipt, update only the matching Todo, and continue the outer loop.

## Contract compatibility

- Treat the User Agent envelope and intent graph as the only authority for intent shape and task decomposition. This Skill operates on the active credit-card Todo; it does not reclassify the user's utterance.
- In a mixed request, emit each completed query result before advancing to the credit-card Todo. Keep every Todo's slots isolated unless the graph explicitly declares a shared slot.
- Resolve `card_reference` and `account_reference` through their graph-declared read capabilities. Persist only product/name plus masked tail; re-resolve the customer-owned core identifier on every resumed turn.
- Follow `clarificationOrder` from the current graph version. A resolver upgrade must not leave a required reference outside that order or replace a semantic slot with an internal API field.
- A runtime or provider response that does not match its registered schema is a compatibility failure. Preserve resumable semantic state and return a stable failure code; never reinterpret it as a new intent.

## Domain rules

- Card listing needs no card selection. Card-specific transaction inquiry, activation, loss reporting, limit change, and security settings do.
- Loss reporting requires a graph-declared reason. Do not silently default it.
- Limit change requires the target amount and graph normalizer.
- Security-setting updates first read current settings, apply the semantic patch, then present the complete masked confirmation summary.
- Multiple state-changing card tasks execute sequentially. Completed receipts must survive later clarification or failure.
- For repayment, read the current bill and payment-account balance before asking the user to choose an amount. Present the outstanding amount, minimum payment, due date, masked card, masked payment account, and available balance as one factual review step.
- When the request does not identify a card or payment account, auto-select only when the corresponding owner-scoped resolver has exactly one eligible result; otherwise return bounded masked choices.
- Treat full payment, minimum payment, and a user-entered amount as distinct choices. Resolve full/minimum choices from the same authoritative bill snapshot; never infer or silently default a repayment amount.
- Keep the default payment account visible and let the user change it conversationally before confirmation. Re-read affected facts when the chosen account or bill snapshot changes.
- The final confirmation must name the masked card, exact amount, masked payment account, and expected remaining account balance. A choice action is not authorization to debit.
- Project repayment success only from a consumed, task-bound capability grant and a matching core receipt. Show paid amount, remaining outstanding, masked payment account, paid time, and only a masked repayment reference.
- A missing, pending, mismatched, or failed receipt is not success. Preserve the resumable task or return an explicit failure state instead of a generic completion card.

## Safety and recovery

- Never expose full card numbers, internal card identifiers, or confirmation artifacts.
- Use only providers in the current execution view. Do not substitute a similarly named tool.
- A missing scope, unavailable provider, or schema mismatch fails closed and preserves resumable semantic state.
- Pause, resume, switch, and cancel use structured workflow control, not matching Chinese reply phrases.
- Never bypass explicit user confirmation for L2 actions.
