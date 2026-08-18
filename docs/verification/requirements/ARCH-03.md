# ARCH-03 verification note

The local persistence foundation now defaults to async file-backed SQLite with foreign keys, WAL, a bounded busy timeout, UTC timestamp normalization, and an explicit repository-injected unit of work. The same configuration boundary lazily constructs the optional `postgresql+asyncpg` dialect without changing domain or application ports, exposing database URLs only through a credential- and query-safe snapshot.

This requirement intentionally creates no application tables, ORM records, Alembic revisions, concrete repositories, or seed behavior. DEL-04 retains ownership of those data-bearing persistence concerns.

Machine authority: `ARCH-03.json`. Runtime evidence is generated outside Git.
