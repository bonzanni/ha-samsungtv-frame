# Contributor and AI-assistant guide

## Live-protocol-first doctrine

Before finalizing the implementation contract for any feature or bugfix whose
correctness depends on a TV connection, Samsung command, event, payload, or
timing behavior, probe the exact candidate protocol against an authorized live
TV.

- Record a sanitized behavior matrix in the approved design, implementation
  plan, issue, or pull request, covering the exact request name and
  parameter shape, response/event correlation, returned value shape, timeout or
  error behavior, TV model family, and relevant operating mode. Never record or
  share a host, MAC address, token, private Home Assistant data,
  artwork/content identifier observed from a production device, or raw
  production payload. Synthetic protocol fixtures remain permitted.
- Treat documentation, upstream implementations, and observations from other TV
  models as hypotheses. Use them to design the probe, not as a substitute for
  live evidence on the target TV.
- For a mutation probe, capture the original value first, make the smallest
  reversible change, read it back authoritatively, and restore and verify the
  exact original value even if the probe fails.
- Encode the confirmed behavior as a failing automated regression before writing
  runtime implementation code.
- If an authorized target TV is unavailable, mark the protocol contract
  provisional and do not describe the feature as implementation-ready or
  release-ready.
