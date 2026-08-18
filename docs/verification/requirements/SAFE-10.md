# SAFE-10 verification

Status: verified locally

The repository now ignores local secrets, databases, key material, caches, dependency trees, and test output in both Git and Docker contexts. `.env.example` is loopback/mock-only with blank optional credentials. A tracked-tree and branch-diff scanner reports high-confidence credentials without printing their values. Central configuration projections mask sensitive fields and secret string forms.

The local digest key is generated with exclusive creation and mode `0600`, survives restart without overwrite, and fails closed when existing state has no key, the stored fingerprint mismatches, permissions are unsafe, or the key path is a symlink.

Verification uses a runtime-generated canary; no realistic token is committed. SAFE-11 separately proves cross-language and container no-egress enforcement.
