# Dual device addressing: stored IP plus hostname fallback

**Status:** idea/backlog. Not implemented. The discovery results fill IP
addresses today; the CLI scanner also reports hostnames.

## Why

DHCP can change a Wallbox or Shelly address. A hostname is the stable identity,
but a DNS lookup on every poll is unnecessary when the last known IP still
works. Store both:

- prefer the last known **IP** for normal polling (no DNS lookup);
- if it stops responding, resolve the **hostname**;
- if the hostname now resolves to a different IP, try that address, verify the
  device identity, and update the stored IP after a successful connection.

The existing link-health history is the natural trigger for the fallback.

## Identity embedded in the DNS name

On this network the hostname already carries the device identity:

- **Wallbox:** `WBxxxxxx`, where `xxxxxx` is the serial number.
- **Shelly EM:** the hostname contains the 12-digit hexadecimal MAC address.

That is useful in two ways:

1. discovery can attach an identity to the IP/hostname pair immediately;
2. a fallback can verify that the address it resolved still belongs to the
   expected device, without trusting an arbitrary reverse-DNS name.

The runtime check should normalise the values before comparison:

- Wallbox: compare the six-character serial, case-insensitively; strip the
  `WB` prefix from the hostname.
- Shelly: keep only hexadecimal characters and compare the 12-digit MAC
  case-insensitively; accept separators such as `:` or `-`.

The stored identity is the source of truth. A hostname-embedded identity, the
stored identity and the device-reported identity should all agree. If they do
not, reject the candidate address and log why.

The Wallbox serial is already in `wallbox.serial` for the cloud/OCPP API. The
Shelly MAC is available from `/status`. The address block should persist the
verified identity next to the IP and hostname so later fallbacks can compare
against it.

If the router's hostname does not contain the identity, the device response is
still enough to verify, but the hostname is only an address alias. Do not infer
an identity from a reverse-DNS name that does not contain one.

## Proposed configuration

For each addressable device (Wallbox, Shelly 1, Shelly 2):

```text
address:
  ip: "192.168.1.180"       # last known working address
  hostname: "WB123456.local" # optional stable identity
  identity: "123456"         # serial for Wallbox, MAC for Shelly
  prefer_ip: true            # default
```

Existing `wallbox.url`, `shelly.primary_url` and `shelly.secondary_url` remain
the compatibility fields. On load:

- an IPv4 literal in `url` becomes `ip`; the hostname may be filled by
  discovery or manually;
- a hostname in `url` becomes `hostname`, with `ip` resolved and cached on the
  first successful start;
- if only one value is present, normal behaviour is unchanged.

The config page should show both fields. Discovery should fill both when it can
(the mDNS name or a DNS name that contains the identity), store the verified
identity, and use the **Use** button to save the pair rather than a bare IP.

## Runtime flow

Normal case:

1. connect to the stored IP;
2. on success, reset the failure count;
3. no DNS lookup.

Failure case, after a configurable threshold (for example 3 consecutive polls,
or a link-health failure age of a few minutes):

1. resolve `hostname` in the device's polling thread or a small background
   resolver, never on the 20-second control loop;
2. try each returned IPv4 address;
3. verify the device is the expected one before using it:
   - Shelly: normalise the `mac` from `/status` and compare it with the stored
     MAC and the 12-hex identity in the hostname;
   - Wallbox: normalise the serial and compare it with `wallbox.serial`, the
     stored identity and the `WBxxxxxx` part of the hostname;
   - reject the candidate if any available identity disagrees;
4. on success, update the in-memory address and persist the new `ip` with
   `config.save()`;
5. if both stored IP and resolved addresses fail, report the failure through
   link health and back off before resolving again.

Backoff should avoid a DNS lookup on every failed poll. For example: resolve
once after the threshold, then retry every 60 seconds, or every 5 minutes after
several failures. A successful connection resets the backoff.

## Link-health integration

`poll_health.py` already records successful and failed polls per device. The
resolver can use it rather than maintaining a second failure counter:

- healthy: keep using the stored IP;
- unhealthy for longer than a threshold: trigger one hostname resolution;
- after a successful fallback: record the new IP and a "re-resolved" event;
- after both fail: keep the existing offline state and retry with backoff.

The health row/dashboard should say which address is in use and when it last
changed, for example `Wallbox · 192.168.1.180 (fallback, updated 2 min ago)`.

## Safety and edge cases

- **Device identity check.** Do not persist a new IP merely because it answered
  on port 502 or `/status`. Compare the stored identity with both the
  device-reported identity and any identity embedded in the hostname. A reused
  IP or a stale DNS name must not hijack the configuration.
- **No DNS on the control path.** Resolve in the polling thread or a background
  resolver with a timeout. A slow DNS server must not delay a control decision.
- **IPv4 only.** The current drivers are IPv4; ignore AAAA results unless the
  drivers gain IPv6 support.
- **Multiple A records.** Try them in order, but do not persist one until the
  identity check succeeds.
- **Config writes.** `Config` is a singleton; guard the save and debounce
  repeated updates. Persist only after a successful connection to the new
  address.
- **Manual edits.** A user-entered IP without a hostname is authoritative;
  do not replace it with a reverse-DNS guess. Auto-update only when a hostname
  is configured.
- **Hostname-only devices.** If no IP is known, resolve on startup and cache
  the result. This is the existing behaviour, just made explicit.
- **Static-IP installs.** Leaving `hostname` blank keeps the current
  IP-only behaviour and avoids DNS entirely.
- **Discovery quality.** Reverse DNS is often unreliable. Prefer a hostname
  that contains the Wallbox serial or Shelly MAC, confirm it against the device
  response, and treat any other reverse-DNS name as a suggestion the user can
  edit. A `.local` mDNS name may need mDNS resolution rather than a plain
  `getaddrinfo` call.

## Tests

- backward compatibility: existing URL-only configs keep working;
- IP preferred when healthy, with no DNS calls;
- fallback resolves after the failure threshold and uses the new IP;
- hostname parsing accepts `WBxxxxxx` and a 12-hex MAC with or without
  separators, and normalises case;
- identity mismatch between hostname, stored identity and device response
  rejects the candidate and keeps the stored address;
- discovery adopts an identity only when the device response confirms it;
- persistence happens only after a successful verified connection;
- backoff prevents repeated DNS lookups;
- IPv6 and multiple-address results are handled safely;
- discovery fills hostname and IP together;
- link-health events show the fallback and the updated address.

## Non-goals

- mDNS browsing on every poll;
- dynamic DNS or remote access;
- changing the device drivers' protocol support;
- replacing the current `url` fields in one step.

This is a reliability refinement over the existing single URL, not a new
discovery mechanism. The discovery tool remains the source of the initial pair.
