"""Stable connector-family taxonomy shared by policy and adapter layers."""

EXTERNAL_CONNECTOR_FAMILIES = frozenset(
    {
        "social",
        "newsletter",
        "crm",
        "cms",
        "events",
        "community",
        "spreadsheet",
        "fulfillment",
    }
)
NON_CONNECTOR_FAMILIES = frozenset({"model", "artifact"})
