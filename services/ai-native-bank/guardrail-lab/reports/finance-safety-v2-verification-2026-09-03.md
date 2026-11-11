# Finance Safety v2 Verification Report

Date: 2026-09-03

## Scope

- Production path: `http://localhost:3000/api/ai-bank/v1/agent-runs`
- Policy version: `finance-safety-v2`
- Pinned upstream commit: `cba20eb4151df267dc91cde2ba20a49f6ff9db99`
- Policy assets: 198 semantic anchors, 13 regex patterns, 34 black samples, 16 white samples

## Enforcement verified

- Input Rail checks normalized current input plus at most six redacted prior user turns.
- Output Rail covers standard, FAQ, navigation, ambiguity, clarification, and non-task terminal paths.
- Action Rail runs before confirmation checkpoint/permit creation and again before confirmed execution.
- `block` and `unavailable` fail closed; action blocks activate no capability.
- Public Trace supports input/action/output Rail stages and leaves no running node after a terminal event.
- AI Harbor no longer waits for Agent Runtime at startup, removing the Guardrail/Agent/AI Harbor dependency cycle.

## Fresh verification evidence

- Guardrail Runtime image: `83 passed`.
- Agent Runtime image: `412 tests`, `OK`.
- Public API/client/server Trace/browser Trace: `125 passed`, `0 failed`.
- Pinned upstream corpus:
  - white: 16 allow, 0 block;
  - black: 25 block, 5 review, 4 unavailable, 0 allow.
- Three-service forced recreation completed with Guardrail, Agent Runtime, and AI Harbor all healthy.
- Public safe request: input allow, output allow, terminal `run.completed`, no running UI nodes.
- Public attack request: input block, delivery blocked, terminal `run.completed`, no running UI nodes.
- Direct multi-turn prompt injection and action control-evasion probes both returned block.

The four black-corpus `unavailable` results are safe fail-closed outcomes, not successful classifications. They remain visible as an availability metric and are not counted as `block` or `review`.

## Limitation

No in-app browser instance was available for screenshot-based visual inspection. The same-origin public API, public Trace validator, and browser Trace reducer were exercised instead.
