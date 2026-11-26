# Ubuntu development

Run from `/home/t3/projects/HA-Heat-Robot` in bash. This is a Home Assistant
custom integration, not a standalone server. No Node build, database migration,
Docker container, or local Home Assistant installation is needed for its tests.
The JavaScript cards are shipped as source; packaging is a tar archive.

## Native setup and checks

Verified on this workstation on 2026-11-23: Python 3.15.2 (uv-managed),
system Python 3.14.4, uv, Node 26.13.1/npm 11.20.0, OpenSSH, tar, curl and jq.
`rg` is absent: use `grep`/`find`, or ask before installing system packages.

```bash
uv venv --python python3.15 .venv
uv pip install --python .venv/bin/python pytest ruff
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check custom_components/ tests/
```

Do not recreate an existing environment blindly. Dependencies installed and
validated here: pytest 9.2.0, Ruff 0.17.6 (isolated in ignored `.venv`).
There were no development dependency pins to upgrade. The manifest has no
third-party runtime requirements. Keep Ruff's Python 3.13 target and CI's 3.13
job: they describe supported source compatibility, not a workstation runtime pin.
No older Python installation is required locally. Tests isolate/stub HA imports;
they do not prove compatibility with a running HA release. Hassfest/HACS remain
CI checks; no Docker installation is required for this workflow.

The full suite passed (690 tests, 15.61 seconds) on Python 3.15.2, and Ruff passed.
Allow 120 seconds for local checks on this slower host; investigate failures or
lack of progress rather than changing integration control-loop timings.

## Local build (no deployment)

```bash
mkdir -p /tmp/truetemp-build
tar --exclude=__pycache__ --exclude='*.pyc' --exclude='*.pyo' \
  -czf /tmp/truetemp-build/truetemp.tar.gz -C custom_components truetemp
tar -tzf /tmp/truetemp-build/truetemp.tar.gz
sha256sum /tmp/truetemp-build/truetemp.tar.gz
```

Only the integration directory is packaged; never package the checkout, `.env`,
agent notes, keys or databases. Review archive contents before any upload.
No services are started by these commands. See the local `docs/deploy.md` for
Linux redeployment, backup, verification and rollback after explicit permission.

## HA-DEV and credentials

The neighboring `/home/t3/projects/ha-dev-env` now uses Proxmox HA OS VMs,
not Docker. Its `instances/truetemp.json` identifies VM 710 on prx2,
`http://10.10.10.24`, and currently lists no mounted integrations. Therefore
`dev-ha.sh -i truetemp restart` does not deploy this checkout and also rewrites
configuration and restarts HA: do not use it as a harmless preflight.
Setup, reset, restore and snapshot commands also mutate the VM.

Private target/key references and current reachability are recorded in
`.agents/memory/deploy-and-release.md`. Use existing local credential files;
never print or copy their contents. Windows token and key paths are historical,
not valid Ubuntu paths. Do not assume the separate `TrueTemp-for-Heat-Pumps`
checkout is this source tree. Confirm the selected checkout for each deployment.
