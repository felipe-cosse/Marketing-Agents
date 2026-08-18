# DOM-05 verification note

Instances are now typed deployment records: one template reference, identity/order/enabled state, ordinal metadata, bounded trigger and connector bindings, optional schedule configuration, and revision. Compiler semantics reject unsupported bindings and require schedule configuration to match one enabled supported trigger.

Machine authority: `DOM-05.json`. Runtime evidence is generated outside Git.
