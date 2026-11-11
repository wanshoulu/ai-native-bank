# Transfer Todo strategies

## Single transfer

Create one Todo, resolve required semantic slots, create the draft, request confirmation, consume the immutable receipt, then complete the Todo.

## Multiple transfers

Create one ordered Todo per recipient. Only the first eligible Todo may enter a state-changing step. After its receipt, re-plan from the remaining Todo state instead of replaying the original plan.

## New payee

Suspend the active Todo at `needs_slots`, project a secure form from the slot contract, and resume the same Todo after the platform returns semantic completion evidence.

## Payee query

Use only the read provider and return masked payee choices. Do not create a transfer Todo unless the delegated intent requests a transfer.
