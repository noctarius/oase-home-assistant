# Local transport research

The cloud integration and local transport share the same `pyoase` O-Net frame
codec. `custom_components/oase_control/local.py` retains an authenticated local
session and is used automatically for supported controls; there is no separate
Home Assistant configuration option.

## Established facts

- The controller discovers clients over UDP port **5959**. O-Net
  `DEVICE_INFO` is packet type `0x1000`; its reply type is `0x10FF`.
- A `TCP_REQ` (`0x1400`) has a seven-byte payload: encryption (`u8`), listener
  port (`u16` little-endian), and Unix time (`u32` little-endian).
- The controller opens a **reverse TLS 1.2** connection to the requested
  listener port. The observed app uses TCP **5999**.
- The controller accepts a self-signed RSA TLS-server certificate with
  `CN=com.oase.easycontrol`; it does not present a client certificate.
- Operational local commands require a successful `PASSWORD_CHECK` (`0x9F00`)
  after TLS. Its 64-byte credential payload is followed by a one-byte reply:
  `1` accepts the credentials and `2` rejects them.
- The corresponding credential is provided by the cloud inventory in the
  gateway's `customAttributesJson` attribute `101`. It is refreshed on every
  cloud inventory poll and cached in the config entry so local control keeps
  working during cloud outages. It is never logged.

## First verification step

With a known controller IP address, call `async_probe_controller(host)`. It
sends a read-only `DEVICE_INFO` O-Net packet directly to that address and
validates a matching reply. It intentionally does not broadcast onto the
network.

`async_open_tls_session(host)` takes a lease on the shared TLS listener for the
port (one listener serves every controller, routing connections by address),
sends the reverse connection request, and returns an `OaseLocalTlsSession`
after the controller connects. Closing the session returns the lease; the
listener stops once no session needs it. The session's `async_request()` method is available for controlled,
read-only O-Net request/reply validation.

## Supported local operations

- Read-only cloud-connection-enabled status as a gateway diagnostic binary
  sensor. It is not a writable control: enabling cloud access requires a cloud
  registration/key lifecycle, and disabling it alone could prevent recovery.
- EGC discovery and supported RDM reads/writes (device on/off and pump power)
- FM-Master live socket-scene reads (`GET_LIVE_SCENE`)
- FM-Master socket and dimmer writes (`SET_LIVE_SCENE`), with a matching,
  affirmative reply required before accepting a write

The local implementation falls back to the cloud relay if a controller cannot
be reached or rejects the local operation. FM-Master socket operation follows
the public O-Net codec but still needs verification against an FM-Master in
this integration's test environment.
