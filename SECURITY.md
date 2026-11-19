# Security policy

nebi (nebi, nebi-cockpit, nebi-cli) is an independent, unofficial project compatible with the Beni robot
(LZ110/LZ100). It is not affiliated with the robot's manufacturer.

## Reporting a vulnerability in nebi

Please **do not open a public issue** for security problems. Report them privately through GitHub's
**"Report a vulnerability"** button (private security advisory) on the project's repository:

<!-- TODO(publish): replace with the real advisory URL, e.g. https://github.com/<owner>/nebi/security/advisories/new -->
`https://github.com/<owner>/nebi/security/advisories/new`

Include the affected package and version, how to reproduce, and the impact you expect. Do not include real robot
serial numbers, identity keys, media tokens, Wi-Fi credentials or packet captures from someone else's device. We aim
to acknowledge reports within a week. Only the latest released version receives fixes.

## Vulnerabilities in the robot itself

Issues in the robot's own firmware, its mobile app or its cloud services are **not** nebi's to fix. Report them to
the robot's manufacturer through its official support / security contact. Please don't file them here, and don't
use nebi to exploit them on devices you don't own.

## Cockpit threat model

`nebi-cockpit serve` is a web server that drives a physical robot. Its defaults assume a single trusted user on
their own computer:

- **Binds `127.0.0.1` by default.** Only processes on the same machine can reach it.
- **Token when bound elsewhere.** With `nebi-cockpit serve --host <non-loopback address>` (e.g. `0.0.0.0`), requests
  from non-loopback peers must present a token (printed at start; `PYNEBI_TOKEN` chooses it) via the
  `X-Pynebi-Token` header, `?token=` or the `pynebi_token` cookie. The page itself (`/`, `/static/*`) loads without
  it so it can pick the token up from its URL.
- **Loopback peers are trusted.** Anything running on the same machine (the `nebi` CLI, local helpers, other local
  users and processes) can control the robot without a token. Don't run the cockpit on a shared multi-user host.
- **Browser protections always apply.** Host-header checks block DNS rebinding; Origin/Referer checks block
  cross-site WebSocket hijacking and CSRF from other web pages you visit.
- **Plain HTTP.** The cockpit doesn't terminate TLS: anyone who can observe the network can read the token and the
  video. **Never expose the cockpit to untrusted networks or the internet.** For remote access, use an SSH tunnel or
  a VPN to a loopback-bound cockpit.
- **Robot link.** The robot's local protocols are whatever its firmware provides. nebi stores its pairing identity
  (`~/.config/pynebi/identity.json`), Wi-Fi credentials (`wifi.json`) and the media token (`media_token`) in your
  home directory. Protect them like passwords.

Physical safety isn't a security boundary: anyone who can reach the cockpit or the agent API can make the robot
move. Supervise it.
