# Syslog Home Assistant app (HL42 fork)

## How to use

This app sends the whole HAOS journal (host, Supervisor, Home Assistant Core,
apps and plugins) to a remote syslog server.

The journal is used as the buffer: an entry is only considered done once it
has been handed to the server. When the server cannot be reached, the app
waits and retries, and sends the waiting entries once it is back. Its position
in the journal is saved in the app's storage, so a restart of the app or of
HAOS resumes after the last entry sent instead of at the end of the journal.

## Configuration

App configuration:

```yaml
syslog_host: syslog.local
syslog_port: 514
syslog_protocol: tcp
syslog_ssl: false
syslog_ssl_verify: true
syslog_format: rfc5424
redact_patterns: []
exclude_patterns: []
backlog_max_hours: 6
max_message_bytes: 8192
```

| key | name | description |
| --- | ---- | ----------- |
| `syslog_host` | Syslog host | The hostname or IP address of the remote syslog server to send HAOS logs to. An IP address avoids depending on DNS to reconnect. |
| `syslog_port` | Syslog port | The port of the remote syslog server to send HAOS logs to. |
| `syslog_protocol` | Transfer protocol | `udp` or `tcp`. Only `tcp` notices that the server is gone; over `udp` lost datagrams are not seen. |
| `syslog_ssl` | SSL encryption | Whether or not to use ssl encryption (only supported with tcp). |
| `syslog_ssl_verify` | SSL verify | Whether or not to verify the ssl certificate. |
| `syslog_format` | Syslog format | `rfc3164` (BSD syslog) or `rfc5424` (ISO 8601 timestamps with time zone, recommended). |
| `redact_patterns` | Redact patterns | Regular expressions (Python syntax) whose matches are replaced by `***` before a message is sent. |
| `exclude_patterns` | Exclude patterns | Regular expressions (Python syntax) matched against `app name: message`; a matching entry is not sent. |
| `backlog_max_hours` | Maximum backlog | After a stop, how many hours back the app may resume (0 to 168). `0` always starts at the end of the journal. |
| `max_message_bytes` | Maximum message size | Largest syslog message sent, in bytes, header included (480 to 65000). A longer one is cut and ends with `...[truncated]`. Set it to what the server accepts. |

### What is sent

- **Hostname**: the HAOS hostname (*Settings → System → Network*).
- **App name**: the container name for containers (`homeassistant`,
  `hassio_supervisor`, `app_…`), the syslog identifier for host programs,
  or their process name when they set none.
- **Process id**: the PID for host programs, `-` for containers (the journal
  only knows the PID of the Docker daemon).
- **Severity**: the journal priority for host programs; for Home Assistant
  Core and the Supervisor, the level written in the line (traceback lines
  keep the level of the line before); for other containers, the level found
  in the usual formats (bashio, s6-overlay, Python logging, logfmt,
  Zigbee2MQTT, Mosquitto, OpenThread Border Router), info
  otherwise. Facility is always `user`.
- **Timestamp**: the time of the journal entry, not the time of sending, so
  entries sent late keep their real time.
- Colour codes are removed from container lines.
- Over TCP, messages are framed by octet counting (RFC 6587).
- A message longer than `max_message_bytes` is cut, on a character
  boundary, and ends with `...[truncated]`. The default, 8192 bytes, is
  what common servers accept without tuning; a container line is at most
  16 KB (Docker splits longer ones), a host journal line can be longer.

### Resuming and its limits

- First start (no saved position): the app starts at the end of the journal
  and sends nothing older.
- Over TCP on Linux, an entry only counts as delivered once the server has
  acknowledged its bytes. The position covers acknowledged entries only; it
  is saved every 5 seconds and when the app stops, after waiting up to
  2 seconds for the last acknowledgements. After a crash or a power loss,
  the entries of the last seconds may be **sent twice**; none is lost.
- A position older than `backlog_max_hours` resumes at that limit; the app
  log says which entries were skipped.
- A TCP connection whose server vanished without closing it is detected
  within about 30 seconds (keepalive and `TCP_USER_TIMEOUT`). The entries
  it had not acknowledged are sent again on the next connection, so the
  server may receive some of them twice.
- With UDP or TLS, acknowledgements cannot be measured: an entry counts as
  delivered once handed to the system, and over TLS the entries of the
  last 30 seconds before a vanished server may be lost.

### Redacting secrets

Some apps write credentials into their logs, for example a secret URL path in
every access log line. Once such a line is sent, it is stored on the remote
syslog server and travels the network in clear text with UDP or TCP without
TLS. `redact_patterns` masks these values on the Home Assistant host, before
the message leaves it:

```yaml
redact_patterns:
  - "/private_[A-Za-z0-9_-]{20,}"
  - "(?<=token=)[^&\\s]+"
```

The first pattern replaces a secret path like `/private_AbC…` completely, the
second one keeps `token=` and masks only the value. Patterns are applied to
the message text in the given order. An invalid pattern stops the app with an
error, so no unmasked messages are sent.

### Leaving entries out

Some programs write lines that are known and useless, and that should not
cost network and storage. `exclude_patterns` leaves them out on the Home
Assistant host. Each pattern is searched in `app name: message` (the app name
as sent: container name or program), before masking, so that a pattern can be
limited to one program by starting with `^name: `. A matching entry is not
sent, but still counts as done: the journal position moves past it, in order.

Example: the hourly reverse lookup sweep that a Home Assistant integration
sends without recursion, which a resolver refusing non-recursive queries
answers with `REFUSED` (753 lines an hour on one host). Only that exact form
is left out: the same lookup failing otherwise, asked with recursion, or from
another program, is still sent.

```yaml
exclude_patterns:
  - '^hassio_dns: \[INFO\] \S+ - \d+ "PTR IN [0-9.]+in-addr\.arpa\. udp \d+ false \d+" REFUSED qr,ra '
```

A left-out entry is invisible on the server by construction. The app writes
once an hour, when it left something out, how many:
`excluded 753 entries matching exclude_patterns in the last 60 min`. An
invalid pattern stops the app with an error.

### App log

The app writes one line when the server becomes unreachable and one when it
is reachable again, never one per message, each starting with the time
(`[18:38:57] WARNING: cannot send to ...`). These lines also reach the syslog
server, under the app's container name.

- `cannot send to host:port (reason)`: the reason is "connection closed by
  the server" when the server ended the connection (a restart), otherwise
  the system's error, for example `Connection timed out` when the server
  vanished and stopped acknowledging data.
- `sending again after N s (M entries not acknowledged, sent again)`: the
  server is back; the M entries it had not acknowledged before the outage
  were sent again, so it may hold some of them twice. Without parenthesis,
  nothing had to be sent again.

## Origin

Fork of [mib1185/ha-addon-syslog](https://github.com/mib1185/ha-addon-syslog)
(Apache License 2.0). Changes are listed in the changelog.
