# Changelog

## 0.8.0

- **BREAKING:** rename integration domain `samsungtv_frame` → `samsung_tv_frame`
  and repository `ha-samsungtv-frame` → `ha-samsung-tv-frame`. There is no
  migration: remove the old integration entry and `custom_components/samsungtv_frame`
  directory, install this version, and re-add the TV via the config flow.
  All services move to the new domain (e.g. `samsung_tv_frame.set_slideshow`);
  automations, scripts, and dashboards referencing `samsungtv_frame.*` services
  or device triggers must be updated. Entity IDs are re-created by the fresh
  config entry. Older changelog entries below retain the historical names.

## 0.7.1

- Treat silently unsupported optional Art capability reads as bounded probes,
  allowing modern-to-legacy fallback without closing a healthy websocket.
- Require same-generation correlated liveness before caching Art settings or
  slideshow dialects, while retaining supervised recovery for ambiguous
  all-silent transports.
- Route slideshow writes through the read-proven generation dialect so older
  Frame firmware does not time out on an unsupported modern command.

## 0.7.0

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
