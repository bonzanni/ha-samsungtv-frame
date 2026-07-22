# Changelog

## Unreleased

- Read all advertised local Art settings as one aggregate, generation-scoped snapshot
  and expose the live-verified Sleep After, neutral motion-sensitivity, and automatic
  brightness-sensor controls without SmartThings.
- Add read-only slideshow state, including duration and category, while keeping
  `samsungtv_frame.set_slideshow` as the single atomic write surface for duration,
  shuffle order, and category.
- Reconcile optional Art state directly after successful local mutations so entity
  state reflects authoritative readback rather than an optimistic value.
- Add strictly allowlisted, zero-I/O Home Assistant diagnostics for integration and Art
  session health without exposing device addresses, credentials, artwork, apps, or raw
  protocol data.
- Intentionally make the existing art brightness and color-temperature entities
  unavailable when the TV is off or their optional Art state is not authoritative for
  the current ready session, instead of exposing an unknown or stale value.

## 0.6.9

- Use a curated built-in app catalog for the media-player source dropdown instead of
  attempting runtime installed-app discovery, whose unanswered websocket requests could
  stall foreground commands. Raw Tizen app ids remain launchable with
  `media_player.play_media`.
- Pair the remote-control channel first during setup, reconfiguration, and
  reauthorization, then validate Art with the same canonical token.
- Reconfigure now requires the TV to show normal TV or app content and the user to
  accept the new Allow prompt.
- Persist a changed remote token synchronously before a successful foreground command
  returns, including before power-off can make the TV unreachable.
- On an ordinary stale remote connection, close the exact captured failed client before
  retrying once with the same credential.
- Route `ms.channel.timeOut` from foreground remote commands through Home Assistant
  reauthorization while preserving the stored token, instead of silently retrying
  without one or treating the event as proof that the credential is invalid. Background
  polling never starts pairing or opens an authorization prompt.
