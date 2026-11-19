# Contributing to nebi

Thanks for helping. nebi is an independent, unofficial project compatible with the Beni robot (LZ110/LZ100). By
contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).

## Development setup

One venv for the whole monorepo, at `nebi/.venv` (Python >= 3.10):

```bash
cd nebi
scripts/dev-setup.sh          # creates .venv and installs nebi, nebi-cockpit, nebi-cli, nebi-voice, nebi-patch editable with [dev] extras
```

Everything works against the built-in simulator, so you don't need a robot. `nebi-cockpit serve --sim` and `nebi up --sim`
start it.

## Before sending a change

```bash
scripts/check.sh              # ruff + mypy + architecture/licence tests + every package's tests; must pass
```

- Keep the dependency direction: nebi never imports nebi_cockpit / nebi_cli, and nebi-cockpit never imports
  nebi_cli (`tests/test_architecture.py` enforces it).
- Keep the joystick dead-man layers and the bit-59 latch invariants described in the package READMEs.
- Test robot-facing changes on the simulator first. When you test on hardware, supervise the robot and give it room.

## Never commit

- packet captures (`*.pcap*`, `captures/`), logs, robot or app dumps;
- serial numbers, device names with serial suffixes, identity keys (`identity.json`), media tokens (`media_token`),
  pairing/latch state (`*_latch.json`), Wi-Fi credentials (`wifi.json`, `.env`);
- downloaded models and rendered audio (`nebi-voice/models/`, `nebi-voice/samples/`);
- firmware images, vendor app packages (`*.apk`, `*.apks`, `*.img`) or other copyrighted vendor files;
- personal IP addresses, SSIDs, hostnames, user names or home-directory paths. Use documentation addresses in
  examples and tests (`192.0.2.x`, `198.51.100.x`, `203.0.113.x`); the robot's own defaults (`192.168.7.1`,
  `192.168.123.1`) are fine.

`.gitignore` covers the usual file names. Check `git diff --cached` anyway.

## Cleaning build leftovers

```bash
scripts/clean.sh              # removes *.egg-info, __pycache__, tool caches, .DS_Store, dist/, build/ (never .venv)
.venv/bin/pip install -q --no-deps -e nebi-lib -e nebi-cockpit -e nebi-cli -e nebi-voice -e nebi-patch   # re-register the editable installs
```

Run it before publishing a release or creating the git repository.

## Licence files

`LICENSE` and `NOTICE` live at the root. Each package keeps identical copies, because wheels can only include
license files from inside the package directory. `tests/test_licensing.py` checks that the copies match the root
files. Edit the root files, then copy them: `for p in nebi-lib nebi-cockpit nebi-cli nebi-voice nebi-patch; do cp LICENSE NOTICE $p/; done`.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs `scripts/check.sh` on every push to `main` and every pull request,
on Linux with Python 3.10–3.14 and on macOS, and builds and smoke-tests the four wheels. To reproduce another
Python version locally: `PYTHON=python3.10 NEBI_VENV=/tmp/venv310 scripts/dev-setup.sh && NEBI_VENV=/tmp/venv310 scripts/check.sh`.

## Releasing

Packages are versioned independently (`version` in each `pyproject.toml`). To release:

1. Bump the versions you changed (and the bounds in dependants, e.g. `nebi-cli` → `nebi>=…,<…`).
2. Check locally: `scripts/check.sh` and `pip install build twine && scripts/build.sh`.
3. Tag and push: `git tag v0.2.1 && git push origin v0.2.1`. The **Publish to PyPI** workflow builds every package
   and uploads the versions that are not on PyPI yet (`skip-existing`).

Dry run first with *Actions → Publish to PyPI → Run workflow → testpypi*.

One-time setup (trusted publishing, so no API token is stored in GitHub). PyPI needs a distinct configuration per
project, so each package publishes from its own GitHub environment:

| PyPI project | Owner | Repository | Workflow | Environment (PyPI) | Environment (TestPyPI) |
|---|---|---|---|---|---|
| `nebi-lib` | `lukaville` | `nebi` | `publish.yml` | `pypi-nebi-lib` | `testpypi-nebi-lib` |
| `nebi-cockpit` | `lukaville` | `nebi` | `publish.yml` | `pypi-nebi-cockpit` | `testpypi-nebi-cockpit` |
| `nebi-cli` | `lukaville` | `nebi` | `publish.yml` | `pypi-nebi-cli` | `testpypi-nebi-cli` |
| `nebi-voice` | `lukaville` | `nebi` | `publish.yml` | `pypi-nebi-voice` | `testpypi-nebi-voice` |
| `nebi-patch` | `lukaville` | `nebi` | `publish.yml` | `pypi-nebi-patch` | `testpypi-nebi-patch` |
| `nebi-ai` | `lukaville` | `nebi` | `publish.yml` | `pypi-nebi-ai` | `testpypi-nebi-ai` |

Add each row as a *pending publisher* on pypi.org (*Your account → Publishing*), and on test.pypi.org with the
TestPyPI environment. The GitHub environments already exist (*Settings → Environments*; optionally require a
reviewer for the `pypi-*` ones).

## Security

Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md), not in public issues.
