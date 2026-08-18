# RUN-02 verification note

The effect-aware planner snapshots catalog records and connector operation metadata supplied atomically by trusted runtime composition, then requires that catalog release label to match the route before it classifies a step. It also snapshots routed instance revisions and effective instance bindings. Read-only plans produce no IDs, clock reads, actions, or approval requests. A plan containing any external write produces every immutable exact-action proposal and matching pending approval request first, then derives an approval-required lifecycle disposition from the retained step effects.

Write commands are typed against the connector registry and snapshotted as canonical JSON. The exact destination token is derived from that command snapshot, payload redaction comes only from trusted registry pointers, and the human destination summary comes only from the effective binding. Caller-supplied labels, effects, redaction schemas, proposal revisions, or destinations cannot enter an approval snapshot.

Three domain-separated hashes retain distinct identities: the structural plan hash excludes runtime and action content; the semantic action hash excludes random and run-local IDs; the existing exact SAFE-02 hash includes the complete plan, proposal, step, and random identity. RUN-05 receives stable key material but remains the sole owner of persistence, key derivation, claims, dispatch, and replay recovery.

The focused network-denied gate exercises a real compiled Community route and the typed connector registry, plus SAFE-02, ARCH-07, RUN-07, and RUN-09 regressions. It does not claim persistence, approval decisions, barrier consumption, dispatch, receipts, audit history, or crash recovery.

Runtime composition is a trust precondition: the content hash and catalog records must come from the same validated `CompiledCatalog`, and the operation records must come from the registry validated against that object. The planner intentionally cannot recompute the catalog compiler's complete content hash from its smaller application snapshot.

Machine authority: `RUN-02.json`. Runtime evidence is generated outside Git.
