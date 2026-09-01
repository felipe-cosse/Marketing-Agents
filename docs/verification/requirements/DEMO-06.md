# DEMO-06 verification

DEMO-06 proves the Email all-approvals-before-any-call barrier. The two exact,
payload-bound action approvals form one authorization set. Before release, and
after only one approval, newsletter, CRM, model, and durable receipt counts stay
at zero. Expired, rejected, reused, unauthorized, drifted, changed-action, and
pre-release cancellation paths release no connector authority.

After both approvals are atomically reserved, a later worker drain invokes the
registered deterministic newsletter and CRM mocks once each with the approved
minimized payloads and stable idempotency keys. It then performs one deterministic
model call and persists the verified onboarding artifact. Replay does not repeat
connector or model effects.

The crash witness is deliberately scoped: a failure after a deterministic mock
receipt is durably committed but before local action success recovers from that
authoritative receipt without repeating the physical mock effect. This does not
claim universal distributed exactly-once delivery for future providers.

The Email gate exercises exact-action recovery for recovery-pending state and
durable-mock-receipt reconciliation. Other stale-action classifications,
lost-claim races, and post-release cancellation races remain lower-level
dispatcher responsibilities and are not claimed as Email end-to-end evidence.

Executable evidence:

```sh
make test-demo-06-backend
```

The gate denies network sockets and runs with temporary local SQLite. Durable
mock receipts are control-plane evidence, not proof of a real subscription, CRM
update, delivered message, real consent, or production outcome. Approval handling
does not dispatch inline, and post-release cancellation cannot roll back an
already durable receipt.

Machine authority: [`DEMO-06.json`](DEMO-06.json).
