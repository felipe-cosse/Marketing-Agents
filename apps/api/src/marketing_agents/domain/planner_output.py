"""Reserved local proposal output identities; never connector execution authority."""

PLANNER_OUTPUT_FAMILY = "planner-output"
PROPOSAL_PREVIEW_KIND = "planner.proposal-preview.v1"
PROPOSAL_PREVIEW_CAPABILITIES = frozenset(
    {
        "cap.newsletter.subscribe",
        "cap.newsletter.unsubscribe",
        "cap.events.enroll-attendee",
        "cap.messaging.send-message",
    }
)
