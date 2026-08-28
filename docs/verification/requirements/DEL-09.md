# DEL-09 implementation evidence

Status: verified

History verification recognizes one legacy merge-subject exception only when all
three configured values match exactly: commit
`ce04fecc475f72c182e61722e69c26af1438669b`, requirement `API-06`, and subject
`[API-06] Merge immutable approval control API`. The approval is deliberately
pinned to the immutable commit identity and exact historical text; it is not a
pattern-based allowance for similarly worded or future merge commits.

No history rewrite was performed. The published commit and its descendants keep
their existing identities, while the verifier classifies only that exact tuple as
the `API-06` requirement merge. The exception changes subject recognition only:
the normal two-parent merge shape, first-parent base, direct non-merge feature
commit, feature subject, nonempty feature tree, merge-base ancestry, tree equality,
unique requirement ID, reachable feature count, and retained requirement-branch
checks remain enforced.

## Verification

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.tooling.test_verify_requirement_evidence`
  exercises exact acceptance plus malformed policy, SHA, requirement-ID, subject,
  and future-commit rejection.
- `PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_requirement_evidence.py history --ref main --allow-incomplete --check-branches`
  validates the complete current first-parent requirement history and retained
  requirement branches with the pinned exception active.
- The manifest runs the same production history verifier against `main` (without
  the optional retained-branch check) so a wrong API-06 SHA, ID, or subject cannot
  pass DEL-09 verification on synthetic tests alone.
- The DEL-09 connection witness reverts both the verifier and exception policy in
  an isolated tree and requires the tooling gate to fail, proving that the test is
  connected to the production control.

## Limitation

The historical API-06 subject remains noncanonical in Git display output. DEL-09
does not rename it or waive any topology rule, and any change to the pinned SHA,
requirement ID, or subject requires a separately reviewed policy update.
