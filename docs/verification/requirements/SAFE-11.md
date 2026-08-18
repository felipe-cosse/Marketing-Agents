# SAFE-11 implementation evidence

Status: implemented; kernel/container no-egress verification pending

Python tests have an autouse socket/DNS guard that allows loopback and blocks non-loopback DNS, TCP, UDP, and `create_connection`. Node tests have matching guards for `net`, HTTP, HTTPS, DNS, and `fetch`. A Playwright-compatible route policy aborts non-loopback browser requests and exposes a zero-external-attempt assertion. Adapter configuration rejects real modes unless external networking and the matching independent opt-in are both enabled.

Runtime canaries pass without making a real connection. This branch does not yet claim the final acceptance condition: prebuilt Python, Node, and browser tests must also run under container/kernel no-egress, and an actual Playwright page must exercise the route guard. AC-15 owns that final proof.
