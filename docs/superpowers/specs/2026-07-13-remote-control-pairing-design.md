# Remote-Control Pairing and Token Ownership Design

## Problem

The integration pairs only `com.samsung.art-app`, stores the token observed
there, and passes that value to both the Art and `samsung.remote.control`
clients. On the production QE65LS03B, the remote endpoint answered that setup
with `ms.channel.timeOut`, so power and key commands could not establish a
remote-control connection. The runtime workaround replaced the remote client
with a tokenless client, but it depended on a foreground Allow prompt and could
lose a newly issued token if the granting command powered the TV off before the
next coordinator heartbeat.

This is separate from the v0.6.8 supervised Art-session work. The Art channel
is healthy and its connection lifecycle must remain unchanged.

## Live protocol decision

A controlled test with the integration unloaded and the TV showing normal
content established all required facts:

- a tokenless `samsung.remote.control` connection displayed an Allow prompt;
- after approval, the TV returned a token;
- that token reconnected to `samsung.remote.control` without another prompt;
- the same token completed the `com.samsung.art-app` connect/ready handshake
  and answered `get_artmode_status`.

Therefore the remote-issued token is the canonical TV credential for this
firmware. Separate Art and remote token fields are not justified.

## Architecture

### Canonical pairing

Configuration and reauthorization pair `samsung.remote.control` first while
the TV shows normal content. They capture its returned token, then validate the
existing Art handshake with that same token. The entry continues to store the
credential under the existing `token` key, so no destructive config-entry
migration or dual-token schema is needed.

Initial setup must never create an entry until both remote pairing and Art
validation succeed. Every temporary client is closed on success, error, or
cancellation.

### Existing entries and reauthorization

Existing entries retain their current token. Setup continues to construct both
clients from it, preserving the working Art path. A successful remote
connection may return a newer canonical token; that token is persisted
immediately and adopted by both clients.

`ms.channel.timeOut` is an indeterminate handshake failure, not explicit proof
of an invalid credential. It must not erase or silently replace the stored
token. A user-initiated remote operation that encounters it starts Home
Assistant's reauthorization flow and raises a clear user-facing error. The
reauthorization form instructs the user to show normal content and approve the
TV prompt; successful reauthorization updates the existing token and reloads
the entry.

No background task may start pairing. The existing `remote_confirmed` gate
continues to prevent the app-list fetch from opening an unconfirmed remote
channel.

### Runtime remote lifecycle

Remove the tokenless runtime client swap and `_remote_tokenless` state. For an
ordinary stale connection failure, capture the current client, close that
client, and retry the command once on a clean connection. Do not retry an
`ms.channel.timeOut` handshake as though it were a stale socket.

After every successful user remote command, inspect the remote client's token
and synchronously notify the coordinator before returning. This ordering is
required for `turn_off`: persistence must finish before the TV can become
unreachable. Successful app-list access may use the same capture hook, while
remaining behind `remote_confirmed`.

The coordinator owns config-entry persistence. Its callback adopts a changed
token in the device, updates the entry, and does nothing when the token is
missing or unchanged. Heartbeat capture remains a compatibility safety net,
not the primary persistence mechanism.

## Compatibility constraints

- Keep the v0.6.8 Art transport, handshake, retry pacing, and supervised
  session behavior unchanged.
- Keep the config-entry `token` key and config-flow version unless Home
  Assistant requires a version bump for the reauthorization step itself.
- Keep the fixed client name `Home Assistant`; the live TV grant and token are
  associated with that identity.
- Never log, expose, or include TV addresses, MAC addresses, or token values in
  errors and diagnostics.
- Never open the remote pairing prompt from a heartbeat or other background
  operation.

## Error handling

- Explicit pairing denial, timeout, connection failure, and Art validation
  failure leave the existing entry unchanged and close all temporary clients.
- Runtime `ms.channel.timeOut` starts reauthorization once and preserves the
  stored credential.
- Ordinary stale remote errors receive at most one reconnect attempt.
- Failed immediate token persistence must fail the foreground operation rather
  than report success with a credential that can be lost on shutdown.
- Entry unload clears token and reauthorization callbacks before closing the
  device so late operations cannot update an unloaded entry.

## Test strategy

Tests must be written and observed failing before implementation. Coverage
must include:

- setup pairs remote first, captures its token, validates Art with it, and
  closes both clients;
- all setup/pairing error and cancellation paths close their temporary
  clients;
- reauthorization preserves the entry on failure and updates/reloads it on
  success;
- `ms.channel.timeOut` starts reauthorization, performs no tokenless retry,
  and never mutates the stored token;
- ordinary stale errors close the captured old client and retry once;
- a token returned by a successful remote command is persisted before that
  command returns;
- unchanged or absent tokens do not update the entry;
- background polling never opens an unconfirmed remote connection;
- the full existing Art and integration test suite remains green.

## Production acceptance

Release the change through the existing HACS custom repository. After the Home
Assistant restart, confirm entry/entity health, complete reauthorization if
requested, execute one full power-off/Wake-on-LAN cycle, verify automatic Art
session recovery to one stable secure socket, and inspect Core logs for remote
pairing, task, or socket leaks.
