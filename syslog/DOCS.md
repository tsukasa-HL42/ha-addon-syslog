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
backlog_max_hours: 6
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
| `backlog_max_hours` | Maximum backlog | After a stop, how many hours back the app may resume (0 to 168). `0` always starts at the end of the journal. |

### What is sent

- **Hostname**: the HAOS hostname (*Settings → System → Network*).
- **App name**: the container name for containers (`homeassistant`,
  `hassio_supervisor`, `app_…`), the syslog identifier for host programs.
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

### Resuming and its limits

- First start (no saved position): the app starts at the end of the journal
  and sends nothing older.
- The position is saved every 5 seconds and when the app stops. After a crash
  or a power loss, the entries of the last seconds may be **sent twice**;
  none is lost.
- A position older than `backlog_max_hours` resumes at that limit; the app
  log says which entries were skipped.
- A TCP connection whose server vanished without closing it is detected
  within about 30 seconds (keepalive and `TCP_USER_TIMEOUT`); entries handed
  to such a connection during that window may be lost.

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

### App log

The app writes one line when the server becomes unreachable and one when it
is reachable again, never one per message. These lines also reach the syslog
server, under the app's container name.

## Origin

Fork of [mib1185/ha-addon-syslog](https://github.com/mib1185/ha-addon-syslog)
(Apache License 2.0). Changes are listed in the changelog.
