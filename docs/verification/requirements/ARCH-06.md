# ARCH-06 verification note

The default LLM composition now returns a credential-free deterministic provider whose immutable registry resolves only exact catalog template/output-schema pairs. Both deterministic and explicitly registered real providers are independently checked against the SAFE-06 schema and bound guard.

Real-provider composition requires the external-network opt-in, independent LLM opt-in, non-empty credential, and an exact case-sensitive factory registration. Missing registration, factory errors, malformed output, and provider-identity changes fail closed without switching to the mock provider.

Machine authority: `ARCH-06.json`. Runtime evidence is generated outside Git.
