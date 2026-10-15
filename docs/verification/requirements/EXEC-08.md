# EXEC-08 — Source-scoped verification handoff

## Scope and authority

Continues from main `a8e197da4ca9f4a7abbc842af737b1c8fbecb2ef` after the
approved EXEC-07 merge, on `req/exec-08-verification-handoff`. The user requested
full verification before a merge request, with CI disabled and every checkpoint
preserved. This record does not authorize a merge or complete DEL-01.
Machine authority: [EXEC-08.json](EXEC-08.json).

## Report and controls

The [current handoff](../../verification.md#exec-08-current-verification-handoff)
lists the changed files, source identities, actual baseline commands/results,
environment and remaining assumptions. Older AC-17, DEL-06, DEL-07 and OBJ-03
records retain their original facts with explicit historical-snapshot notices.
The assumption register now describes EXEC-06's bounded logical retention
maintenance without implying secure-media erasure, backup deletion or a deletion
SLA. No application, migration, connector, network, Docker or CI behavior changes.

`make verify-exec-08` checks bounded reporting contracts only. It never executes
documented examples, authenticates a report, or proves arbitrary prose correct.
Its lightweight owned-fixture regressions exercise omission, identity/scope
drift and false success claims. The existing complete documentation/link checks,
AC-17 negative controls and assumption-register tests remain required. The
connection witness restores only `docs/verification.md` to the base, retaining
the new checker and tests; it must fail for the missing current handoff.

Although EXEC-08 is an allowed carrier for protected evidence files, no change
to the existing policy, schema or evidence executor is needed or included.

## Qualification procedure

Freeze one nonempty primary commit, including its verification commands in the
commit body, before the final runs. Keep the working tree clean and all results
outside Git. Do not amend the source or metadata after qualification; if a new
candidate is needed, preserve the previous checkpoint and qualify the new one.

1. Run focused reporting, existing documentation/assumption regressions and
   static checks before freezing the candidate.
2. Run `python -B -m scripts.ac_16_quality run` with the pinned native toolchain,
   frozen/offline dependencies and no runner overrides. All 20 gates, 17 safety
   modules and 16 browser runners remain required, with original deadlines.
3. Run `python -B -m scripts.verify_requirement_evidence branch --id EXEC-08
   --base main --run --witness`. This executes all six manifest gates, including
   the matching quality audit, complete committed-HEAD clean-state journey and
   separate paired Compose backup/restore, then the restored-document witness.
4. Independently review the actual outputs, source/tree identity, cleanup,
   unchanged checkpoint refs and disabled CI state. Publish only reviewed refs.
5. Report the exact qualified commit/tree, files, commands, pass/fail/skip results,
   assumptions and limitations in the final handoff before requesting the
   explicitly approved non-fast-forward merge.

The source-bound quality record and requirement attestation are local consistency
evidence, not independently authenticated execution. The checked-in handoff
describes inherited baseline evidence and the procedure; its candidate result
status remains conditional on the separately observed external records. It
cannot embed the final hash of a commit that includes itself.

## Preserved limits

- CI is disabled, not green; all existing checkpoint branches are retained.
- DEL-01 remains pending. EXEC-08 is not whole-project completion or production
  release approval, even after its own gates pass.
- Optional PostgreSQL skips are not PostgreSQL qualification. Live providers,
  real delivery, production identity, cloud deployment and production RPO/RTO
  remain unqualified.
- Historical AC-15 isolation reports are not fresh EXEC-08 four-suite results.
  These commands are not a literal `make acceptance` invocation.
- Acquisition may reach registries; network-none test phases and local-origin
  browser checks do not remove the web ingress container's OS outbound route.
- Retention maintenance is bounded logical field erasure, not secure erasure of
  media or backups, and TTL eligibility is not a deletion SLA.
- Failed/interrupted attempts remain failed/interrupted. Subtest/report-event
  counts are not unique test counts. A consistency-only record is not proof of
  executed behavior without observation, and a passing documentation checker is
  not a product qualification.
- No secret, database/key bundle, raw payload, full prompt or personal data belongs
  in verification artifacts or Git. Owned test cleanup does not remove reusable
  dependency/image caches or unrelated developer data.
