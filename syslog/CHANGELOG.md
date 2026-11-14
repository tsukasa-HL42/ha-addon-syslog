# Changelog

## 1.1.0

- The reason given in `cannot send` is the real one: "connection closed by
  the server" only when the server ended the connection; otherwise the
  error the system recorded, such as a reset or `Connection timed out` when
  the server stopped acknowledging data. Seen on HAOS: a server that had
  vanished was reported as having closed the connection
- `sending again` says how many entries the server had not acknowledged and
  were sent again: `sending again after 15 s (4 entries not acknowledged,
  sent again)`
- A line without syslog identifier gets the name of its process as app name
  (`udisksd` on HAOS), instead of no name at all
- New option `exclude_patterns`: regular expressions matched against
  `app name: message`; a matching entry is not sent, the journal position
  still moves past it in order, and the app says once an hour how many it
  left out. Example in the documentation: the hourly reverse lookup sweep
  refused by the resolver on HAOS

## 1.0.0

First stable release of the HL42 fork, the code of 0.6.0-hl.3 unchanged.
Major version: the forwarder is rewritten, the image moves to Debian 13, the
app is built locally, and the delivery guarantees differ from upstream 0.6.0.

Proven on a running HAOS host, read on the receiving server:

- update of the app (0.6.0-hl.2 to 0.6.0-hl.3): no second without entries
- reboot of HAOS: the shutdown and the whole boot of HAOS received, the
  kernel included, written before the app was running again
- server vanished without closing the connection, for 6 min 38 s: detected
  after 33 s; on return, the whole backlog received with its own times and
  no gap (a message written every 10 s present without exception); with
  0.6.0-hl.2, the 31 s following the outage were lost

## 0.6.0-hl.3

- No loss when the server vanishes without closing the connection: over
  TCP on Linux, an entry only counts as delivered once the server has
  acknowledged its bytes (`SIOCOUTQ`); after a failure, the entries it had
  not acknowledged are sent again first, in their order. Measured before:
  31 s of entries lost when the server vanished; now duplicates instead
- The saved position only covers acknowledged entries; on stop, the app
  waits up to 2 s for the last acknowledgements
- A connection closed by the server is reported and reopened like any
  other failure, so that its unacknowledged entries are sent again
- The app's own lines start with the time (`[18:38:57] WARNING: ...`)
- UDP, TLS and systems without `SIOCOUTQ` keep the previous behaviour

## 0.6.0-hl.2

- Severity read from two more formats seen on a real HAOS host:
  OpenThread Border Router (`6d.00:27:52.159 [D] ...`, letters C, W, N, I, D)
  and Mosquitto plugins (`error: ...`, lower-case level and a colon first)
- Documentation: app containers are named `app_...` since the add-on to
  app rename

## 0.6.0-hl.1

Fork of mib1185/ha-addon-syslog 0.6.0. `journal2syslog.py` is rewritten
around the same formats; `config.yaml`, `run.sh`, `DOCS.md` and the
translations are modified.

- No loss on outages: an entry is retried until the server takes it, the
  journal being the buffer; a connection closed by the server is noticed
  before writing to it
- Resume after a restart: the journal position is saved in `/data` (every
  5 s and on stop) and the app resumes after the last entry sent, at most
  `backlog_max_hours` back (new option, default 6)
- Detect dead TCP servers within about 30 s instead of about 15 min (socket
  timeout, keepalive, `TCP_USER_TIMEOUT`), and never keep a closed socket
  after a failed connect; from tb-dev-ops, upstream pull request #52
- New option `redact_patterns` to mask secrets before sending; from Milas22,
  proposed upstream in issue #45
- Timestamp of the journal entry instead of the time of sending
- Severity: journal priority `notice` kept; level read from the usual formats
  (bashio, s6-overlay, Python logging, logfmt) for apps and plugins
- App name of container lines is the container name (from bombadi), process
  id `-` for containers
- Combined colour codes such as `\x1b[1;31m` are removed (from w531t4)
- RFC 3164: space-padded day; octet-counting framing over TCP for both formats
- An unresolvable or unreachable server is retried instead of stopping the
  app; one log line when the server becomes unreachable, one when it is back
- `run.sh` starts the forwarder with `exec`, so that it receives SIGTERM and
  saves its position on stop
- Built locally by the Supervisor (no prebuilt image), base image moved from
  Debian 12 to Debian 13 and pinned to `base-debian:trixie-2026.11.0`
  (python-systemd 235 in both, Python 3.13); the build restores the execute bit and LF
  line endings of the scripts, which a copy from Windows may lose
- Offline test bench in `tests/`
- Removed what the fork does not use: publishing workflows, Dependabot,
  release drafter, development container, funding link and the
  `bashio_info.sh` debug helper

## 0.6.0

- Drop support for the deprecated `armhf`, `armv7` and `i386` architectures
- Build with Docker BuildKit and the new Home Assistant builder actions
- Publish a multi-arch image to GitHub Container Registry (`ghcr.io/mib1185/ha-addon-syslog`)

## 0.5.2

- Rename add-on to app

## 0.5.1

- Fix forwarder crash on journal messages with invalid UTF-8 or repeated fields
- Fix forwarder crash on journal entries with an invalid priority

## 0.5.0

- Add option to choose between RFC3164 (BSD) and RFC5424 syslog format

## 0.4.1

- Fix tagging of containers

## 0.4.0

- Handle unavailable syslog server

## 0.3.0

- Add tls support

## 0.2.0

- Determine correct syslog level for ha core, supervisor and haos host messages

## 0.1.0

- Use pre-built images

## 0.0.2

- Make messages RFC3164 (bsd) compliant

## 0.0.1

- Initial version
