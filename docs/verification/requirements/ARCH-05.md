# ARCH-05 verification note

The orchestration composition boundary now depends only on injected UTC clocks, namespaced ID generators, domain-typed repositories, and an async unit of work. Static import checks prevent domain/application layers from reaching frameworks or outward adapters.

Machine authority: `ARCH-05.json`. Runtime evidence is generated outside Git.
