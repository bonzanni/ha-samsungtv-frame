# Changelog

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
