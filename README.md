# OASE Control for Home Assistant

Cloud-first custom integration for OASE InScenio / FM-Master controllers. It is
built on [`pyoase`](https://github.com/deltasystems-pl/pyoase), so outlet and
EGC/RDM commands use the same O-Net frame codec that can later support a local
transport.

## Current scope

- OASE Cloud account sign-in (Azure AD B2C via `pyoase`)
- 30-second inventory polling
- Gateway availability and type
- FM-Master sockets 1–3 as switches
- Dimmable outlet as a brightness light
- Attached EGC devices' own on/off state, when the cloud reports it as controllable
- Attached EGC pump power as a 0–100% number entity

The integration deliberately uses OASE Cloud for transport. It does not make
direct LAN connections yet: the controller's local protocol requires UDP
discovery and a reverse TLS connection, whereas the cloud relay is reliable and
already uses identical O-Net payloads.

## Install during development

Copy `custom_components/oase_control` into Home Assistant's
`config/custom_components/` directory and restart Home Assistant. Then add
**OASE Control** in Settings → Devices & services and sign in with the OASE
Control account.

The config entry presently retains the email/password so `pyoase` can renew the
OAuth session. Treat Home Assistant backups and `.storage/core.config_entries`
as sensitive.

## Development tests

Create a virtual environment using the Python version supported by your Home
Assistant release, install `requirements_test.txt`, then run `pytest`.
