---
name: transfer-remittance
description: Use for CCB payee lookup and transfer/remittance tasks, including one or many recipients, missing payee details, amount-scope ambiguity, sequential confirmation, pause, resume, cancellation, and receipt continuation.
---

# Transfer and remittance

Use the active `IntentTaskEnvelope`, graph-provided `slotContract`, task policy, and restricted execution view as the only authority. Never invent a capability, slot, provider, internal identifier, or business rule.

## Operating loop

1. Convert every delegated business goal into one Todo item. Preserve task order and dependency edges.
2. For each active Todo, observe its current slots and prior immutable receipts.
3. Resolve semantic references only through the capability declared by the slot relation.
4. Ask only for required unresolved slots, following `clarificationOrder`. Optional purpose/remark never blocks progress.
5. Plan only capabilities present in the current restricted execution view.
6. Check the planned call against the graph contract and risk boundary before invoking it.
7. After an invocation, record the result against the active Todo, re-observe the Todo list, and select the next eligible task.
8. Finish only when every Todo is completed, cancelled, or explicitly blocked.

## Multiple recipients and amounts

- One transfer goal per recipient. Do not merge recipients into one task.
- Adjacent recipient names may omit punctuation or conjunctions; preserve every entity boundary already supplied by the validated envelope and never concatenate them into one recipient.
- If the validated envelope holds fewer transfer goals than the user's clarified answer implies (for example, two payee names arrive while the active goal declares a single `payee` slot), submit `status=replan_required` with `proposed_tasks`: one proposal per recipient, each carrying the same graph `intent_id` and that recipient's `payee` slot value. Never invent an intent id, never mix intents in one proposal, and never stuff two recipients into one `payee` value or into a hand-built Todo; the platform rebuilds the structure and projects the amount-scope question.
- Execute state-changing transfer tasks sequentially.
- If one amount is paired with multiple recipients and scope is not explicit, clarify whether it is per recipient or a total. Do not guess.
- Shared semantic slots may be reused only when the envelope marks them shared. Keep recipient-specific slots on their own Todo.
- A failure or clarification on a later task must not erase the receipt of an earlier completed task.

## Payee handling

- Use the payee resolver for saved payees.
- When a payee is not saved, request a secure structured form with known semantic values prefilled. Full account numbers remain outside model context and Trace.
- Saving a new payee is optional and requires explicit user selection.
- Never infer a payee identifier from a name alone.

## Confirmation and execution boundary

- Creating a transfer order is a draft operation, not money movement.
- Return a confirmation checkpoint with a masked summary when the graph requires confirmation.
- The model must not authorize or execute money movement. Those actions belong to the user-action and controlled system boundary.
- Treat confirmation artifacts as opaque. Never display, persist in Todo text, or replay them.
- After a confirmed receipt returns, mark only that Todo complete and continue the outer loop.

## Pause, switch, resume, and cancel

- Follow structured workflow control supplied by the platform.
- On pause or intent switch, persist semantic state and Todo cursor without calling another business capability.
- Resume only the referenced workflow revision; never select a workflow from reply wording.
- Cancellation applies to the referenced active Todo or workflow scope and must not cancel completed receipts.

Read [task strategies](references/task-strategies.md) for Todo patterns and [exception playbook](references/exception-playbook.md) for recovery behavior.
