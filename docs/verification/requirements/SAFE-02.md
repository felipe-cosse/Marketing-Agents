# SAFE-02 verification note

Mutating connector ports now accept only an internal sealed authorization created from an atomically reserved exact approval. The guard binds action content, scope, capability, connector binding, and idempotency identity; a raw action or boolean approval cannot enter the port.

Machine authority: `SAFE-02.json`. Runtime evidence is generated outside Git.
