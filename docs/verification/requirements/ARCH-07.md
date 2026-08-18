# ARCH-07 verification note

The connector boundary now has capability-specific DTOs and async protocols for social, newsletter, CRM, CMS, events, community, spreadsheet, and fulfillment integrations. An immutable 20-operation registry cross-checks the compiled catalog, exposes deterministic no-network mocks, and disables the two unassigned v1 writes.

Every mock write independently rechecks the existing SAFE-02 sealed exact-action proof. Reserved CMS and fulfillment mutations have no registered execution path, and real connector mode remains explicitly unimplemented rather than falling back to mocks.

Machine authority: `ARCH-07.json`. Runtime evidence is generated outside Git.
