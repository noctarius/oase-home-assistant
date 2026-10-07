# Local transport research

The cloud integration and a future local transport share the same `pyoase`
O-Net frame codec. `custom_components/oase_control/local.py` contains the
experimental local session setup; it is not exposed in the Home Assistant UI
yet.

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
  gateway's `customAttributesJson` attribute `101`. It is kept in memory only.

## First verification step

With a known controller IP address, call `async_probe_controller(host)`. It
sends a read-only `DEVICE_INFO` O-Net packet directly to that address and
validates a matching reply. It intentionally does not broadcast onto the
network.

`async_open_tls_session(host)` starts a temporary TLS server, sends the reverse
connection request, and returns an `OaseLocalTlsSession` after the controller
connects. The session's `async_request()` method is available for controlled,
read-only O-Net request/reply validation.

## Unresolved before local control

Before adding a local config option or any write path, we need to validate:

1. the first local encrypted O-Net request/reply exchange from Home Assistant;
2. stable key/certificate storage suitable for Home Assistant;
3. reads for each planned local entity;
4. writes only after their matching read path works.

Until those are verified, cloud remains the only production transport.
