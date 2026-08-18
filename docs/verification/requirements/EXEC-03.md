# EXEC-03 verification note

The CI dependency graph now makes the catalog job a hard prerequisite. That job verifies the committed semantic release lock and runs the complete catalog suite before backend or governance jobs. A separate parser rejects missing dependencies, missing commands, duplicates, and reordering.

Machine authority: `EXEC-03.json`. Runtime evidence is generated outside Git.
