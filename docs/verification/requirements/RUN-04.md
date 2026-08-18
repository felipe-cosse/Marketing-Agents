# RUN-04 verification note

Work admission now binds the complete normalized source, event, instance, trigger, workflow, execution mode, campaign-brief revision, instance-configuration revision, and canonical payload to a restart-stable installed key. Both the admitted-payload digest and full admission digest are domain-separated HMAC-SHA256 values; key material and digest values are excluded from diagnostic representations.

The portable `work_items` record enforces one `(source,event_id,agent_instance_id)` row. The repository inserts before reading and contains uniqueness recovery in a savepoint, while the outer unit of work establishes the physical SQLite transaction so `admit_in_uow` cannot accidentally commit. File-backed SQLite tests use independent sessions and distinct candidate IDs to prove identical races resolve to created plus replayed, changed races resolve to one creator plus one collision, restart returns the original work, and caller rollback removes the row.

Machine authority: `RUN-04.json`. Runtime evidence is generated outside Git.
