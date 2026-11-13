# Changelog

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
