# OASE Control for Home Assistant

![OASE logo](OASE_Logo_Standard_rgb.svg)

Custom integration for OASE InScenio / FM-Master controllers. It is built on
[`pyoase`](https://github.com/deltasystems-pl/pyoase) for cloud inventory and
O-Net frame handling.

## Current scope

- OASE Cloud account sign-in (Azure AD B2C via `pyoase`)
- 30-second cloud inventory polling and 5-second local status polling
- Gateway availability and type
- FM-Master sockets 1–3 as switches
- Dimmable outlet as a brightness light
- Attached EGC devices' own on/off state
- Attached EGC pump power as a 0–100% number entity
- Local-first reads and writes for FM-Master outlets and discovered EGC devices;
  cloud relay fallback when local communication is unavailable

The initial inventory and local controller credential come from OASE Cloud.
After setup, the integration discovers the controller on the LAN and uses its
authenticated reverse-TLS connection for FM-Master outlet state and controls,
as well as EGC status updates and supported pump controls. It falls back to the
cloud relay if the local controller cannot be reached.

Local-transport details are tracked in
[docs/local-transport.md](docs/local-transport.md).

## Install with HACS

1. Open **HACS** in Home Assistant.
2. Select **Integrations**, then the three-dot menu and **Custom repositories**.
3. Add `https://github.com/noctarius/oase-home-assistant` with category
   **Integration**.
4. Search for **OASE Control**, install it, and restart Home Assistant.
5. Add **OASE Control** from Settings → Devices & services.

The repository follows HACS's standard integration layout:
`custom_components/oase_control/`. It can be installed directly as a custom
repository now; inclusion in HACS's default catalog is a separate review step.

## Install during development

Copy `custom_components/oase_control` into Home Assistant's
`config/custom_components/` directory and restart Home Assistant. Then add
**OASE Control** in Settings → Devices & services and sign in with the OASE
Control account.

The config entry presently retains the email/password so `pyoase` can renew the
OAuth session. Treat Home Assistant backups and `.storage/core.config_entries`
as sensitive.

## Logo attribution

The OASE logo is sourced from
[Wikimedia Commons / Wikipedia](https://de.wikipedia.org/wiki/Datei:OASE_Logo_Standard_rgb.svg).

## Development tests

Create a virtual environment using the Python version supported by your Home
Assistant release, install `requirements_test.txt`, then run `pytest`.
