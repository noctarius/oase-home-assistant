# Local transport research

The cloud integration and a future local transport share the same `pyoase`
O-Net frame codec. This branch adds a small, read-only probe in
`custom_components/oase_control/local.py`; it is not exposed in the Home
Assistant UI yet.

## Established facts

- The controller's local endpoint uses UDP port **5959**.
- O-Net `DEVICE_INFO` is packet type `0x1000`; its reply type is `0x10FF`.
- The controller later opens a **reverse TLS** connection to a listener on TCP
  port **5999**. The Home Assistant host must therefore be reachable from the
  controller.

## First verification step

With a known controller IP address, call `async_probe_controller(host)`. It
sends a **candidate** read-only `DEVICE_INFO` O-Net packet directly to that
address and validates a matching reply. It intentionally does not broadcast
onto the network. Successful hardware validation is required before treating
this packet as the real discovery request.

## Unresolved before local control

The reverse TLS handshake must be observed with real hardware before adding a
local config option or write path. In particular, we need to capture:

1. the full UDP request that asks the controller to connect back;
2. any controller password/MAC/host-address payload required by that request;
3. the accepted TLS versions and ciphers in a Home Assistant-supported runtime;
4. the first encrypted O-Net request/reply exchange.

Until those are verified, cloud remains the only production transport.
