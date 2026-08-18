# DEL-02 verification note

The complete catalog is shipped with a strict semantic release lock: schema/format version, catalog content version, domain-separated hash, exact global counts, and exact department distribution. A standalone command recompiles the catalog and fails on any drift or unknown lock field.

Machine authority: `DEL-02.json`. Runtime evidence is generated outside Git.
