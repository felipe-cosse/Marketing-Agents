# OBJ-04 credential-free local runtime

Requirement: run locally without cloud credentials or real external calls.

Status: implemented and locally verified within the boundaries below. Final
committed-feature gates and the restored-base witness are required before merge.

## System change

The native launcher's prerequisite checks previously inherited the entire host
environment before runtime mock/offline settings were applied. The installed
`pnpm` is a Corepack shim: with a cold cache its `--version` operation can acquire
the pinned package manager. The defect was in tooling acquisition before startup,
not evidence of a real model or connector invocation.

`scripts/dev.py` now uses the same explicit allowlisted tool environment for
version checks and supervised processes. It disables Corepack network access,
download prompts and automatic pinning, plus pnpm update notifications and online
acquisition. It preserves public local cache paths without copying credential,
proxy, or loader variables. Version checks use the repository working directory;
the pnpm check uses the selected Node executable's directory first on PATH.
Missing installed/cached tools produce the existing safe bootstrap instruction.
No automatic install or retry with external-network opt-in is added.

The native entrypoints still force local identity, mock model/connectors and
external-network opt-out. Existing runtime composition rejects unsupported live
settings. Compose backend isolation and adapters are unchanged.

Qualification also exposed a shutdown race: a process-group existence probe can
temporarily return permission denied while owned processes are exiting. The
supervisor now retries that inconclusive result within its existing grace bound,
requires confirmed disappearance, and never re-signals a group already known
absent. Persistent denial remains a safe shutdown failure; it is not classified
as a missing process. Cleanup still targets only recorded owned groups and closes
logs even on an error.

## Evidence

- Five focused environment tests cover prerequisite/runtime parity, repository
  authority, explicit Node selection, preserved caches and safe failure messages.
  On the old implementation, the initial controls failed on inherited version
  environments and absent offline flags (two failures, one passing control).
- A real installed Corepack cold-cache probe invokes the production version helper
  with synthetic ambient credentials. Corepack itself refuses acquisition before
  any guarded Node transport attempt, leaves package metadata unchanged and
  downloads no cache files. Missing/unpinned tooling fails; it is not skipped.
- The actual native supervisor initializes fresh storage with an empty home and a
  separately declared prewarmed package-manager cache. Synthetic real-mode opt-ins,
  proxy/loader settings and credentials must disappear at every child launch.
  API, run worker, scheduler and Vite reach readiness; all five demos complete
  through Vite's web origin. Health alone uses the API origin because Vite does
  not proxy the readiness endpoint.
- The email demo requires both approvals before model/connector calls. Restarting
  the same supervisor installation replays the five intake keys without changing
  the digest-key fingerprint, persisted counts or original action identities;
  exactly two mock connector receipts remain. Every owned process group is gone
  after each normal shutdown.
- Test-only preloaders instrument each real Python worker module and controller.
  Five socket/DNS canaries are denied before OS delegation; external attempts are
  counted even if application code swallows their errors. A deliberately disabled
  primary guard is caught by a second in-memory tripwire, not a real network call.
- Cleanup controls simulate clean, crashed and timed-out supervisors with leaked
  child groups. The harness always inspects recorded owned groups, forcibly cleans
  survivors if necessary and still fails the test; it never touches unowned groups
  or treats recovery as successful normal shutdown.
- Existing mock-default, provider/connector, Compose contract, clean-state harness,
  native supervision and Python/Node network-control suites remain regression gates.

Preflight passed the **153-test** runtime/adapter/tooling selection and the
**two-test** real native lifecycle/guard-control run (44.76 seconds). The installed
Corepack cold-cache probe separately passed. The final manifest adds guard and
cleanup controls to the regression selection and invokes both native-only
scenarios through the same public Make target. These are local results, not
whole-system acceptance.

One subsequent sanitized native preflight completed both demo/replay generations
but the second shutdown returned `native_process_start_failed`, the production
entrypoint's generic OSError message. Inspection found no surviving recorded child
groups, but the original log could not establish an errno or call site. Test-only
tracing was added to retain bounded function/line/exception-type/errno categories
without exception text, arguments, or environment values. An instrumented pass
(45.64 seconds) did not explain the failed result, so bounded reproduction
continued. The next run captured `PermissionError`, errno 1, at
`Supervisor.close`'s `os.killpg(child.process.pid, 0)` probe; the harness's cleanup
probe also encountered this error. All recorded groups subsequently disappeared.

That confirms the failing operation and the supervisor's missing transient-error
handling, not the underlying macOS permission condition. Deterministic controls
must establish retry-until-confirmed-absent behavior and rejection of persistent
denial. Both complete native lifecycle generations and the final committed-feature
gates remain mandatory after the fix; no failed result is waived.

After the bounded retry change, the sanitized **164-test** regression selection,
network controls, lint, formatting, documentation check and environment witness
passed. The sanitized public native gate also passed in **44.19 seconds**: both
generations completed and all fourteen recorded owned groups were confirmed
absent. That real run recorded no EPERM; the deterministic transient/persistent
and post-force controls establish retry behavior without claiming that the host's
underlying permission condition was reproduced or eliminated by the final run.

## Reproduce

Activate the repository's installed Python 3.12 virtual environment and pinned
Node 24.20.0/Corepack/pnpm 11.24.0 on PATH, after explicit bootstrap:

```text
make verify-native-offline
python3 -m scripts.verify_obj_04_environment
make verify-requirement REQUIREMENT=OBJ-04 BASE=3bed5c2e99b17f61bb9663503ea23885acd329b1 HEAD=<feature-commit> PYTHON=python3
make verify-history PYTHON=python3
```

The native-only files deliberately do not use pytest's default `test_` filename
prefix: the backend verification image has no Node/Corepack/Vite. The explicit
Make target is a required OBJ-04 gate and must fail if native prerequisites are
absent. It performs no dependency installation. `COREPACK_HOME` may select a
prewarmed cache; otherwise the native test finds the current account's standard
Corepack cache independently of the verifier's empty HOME. No credential profile
is copied into the fresh home.

All committed-feature gates run under the evidence verifier's sanitized
environment. Local server and Unix-socket binds need the host's loopback execution
permission. The dependency-free witness also runs in an isolated Git archive:
restoring only `scripts/dev.py` to the base must fail by an ordinary assertion
that the version process inherited its environment, not by an import or missing
dependency error. The attestation binds successful gates to the feature tree.

## Limits

This instruments Python socket/DNS interfaces, not arbitrary native OS egress.
Vite receives the verified environment but its Node network APIs are not covered
by the Python lifecycle guard. The separate Corepack probe covers the real shim's
cold-cache refusal using the existing Node guard. Browser request guards and
Compose backend `network_mode: none` remain separate evidence, not substitutes
for one another.

Bootstrap and image builds can require registry access; offline runtime is not
offline acquisition. Installed tool binaries, dependencies and host filesystem
administrators remain trusted. Retaining a cache path is not loading a credential.

The five-demo API journey does not qualify browser interactions, every catalog
workflow, optional real providers, or real delivery. The existing Compose static
web ingress container retains an OS-level outbound route. No new full
`make verify-clean` committed-export Compose run is claimed here; its harness
tests and historical DEL-05 result are narrower evidence.

CI stays disabled, workflow files are untouched and no branches are pushed.
