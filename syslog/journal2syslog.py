"""
Forward the HAOS systemd journal to a remote syslog server.

The journal itself is the buffer: the reader only moves past an entry once it
has been handed to the server, and its position is saved in /data so that a
restart resumes where it stopped instead of at the end of the journal.

Configuration comes from environment variables set by run.sh.
"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from systemd import journal

FACILITY_USER = 1
NILVALUE = "-"
BOM = "\ufeff"
STATE_FILE = "journal_position.json"

# Bound how long an unreachable or vanished syslog server can block the
# forwarder or silently swallow messages. Without these, a TCP peer that
# disappears without a reset (e.g. a restarted container behind NAT) keeps
# sendall() "succeeding" into the kernel send buffer until TCP gives up
# retransmitting (~15 min on Linux), and connect()/the TLS handshake to an
# unresponsive server can block indefinitely.
# (from tb-dev-ops, upstream pull request #52)
SOCKET_TIMEOUT = 10  # seconds, for connect, TLS handshake and send
TCP_KEEPALIVE_IDLE = 30  # seconds idle before the first keepalive probe
TCP_KEEPALIVE_INTERVAL = 10  # seconds between keepalive probes
TCP_KEEPALIVE_COUNT = 3  # unanswered probes before the connection is dropped
TCP_USER_TIMEOUT = 30_000  # ms that sent data may stay unacknowledged

# Severity names found in log lines, mapped to syslog severities (RFC 5424).
LEVEL_TO_SEVERITY = {
    "EMERG": 0,
    "PANIC": 0,
    "ALERT": 1,
    "CRITICAL": 2,
    "CRIT": 2,
    "FATAL": 2,
    "ERROR": 3,
    "ERR": 3,
    "WARNING": 4,
    "WARN": 4,
    "NOTICE": 5,
    "INFO": 6,
    "DEBUG": 7,
    "TRACE": 7,
}
DEFAULT_SEVERITY = 6

# Home Assistant Core and Supervisor: "2026-11-13 10:00:00.000 ERROR (...)".
# Lines without a level (tracebacks) keep the level of the previous line.
PATTERN_LOGLEVEL_HA = re.compile(
    r"^\S+ \S+ (?P<level>INFO|WARNING|DEBUG|ERROR|CRITICAL) "
)
HA_CONTAINERS = {"homeassistant", "hassio_supervisor"}

# Other containers (apps, plugins): the usual formats, first match wins.
GENERIC_LEVEL_PATTERNS = (
    # bashio: "[12:34:56] INFO: message"
    re.compile(
        r"^\[\d{2}:\d{2}:\d{2}\] (?P<level>TRACE|DEBUG|INFO|NOTICE|WARNING|ERROR|FATAL):"
    ),
    # s6-overlay: "s6-rc: warning: ..."
    re.compile(r"^s6-[\w-]+: (?P<level>info|warning|error|fatal):"),
    # Home Assistant style: "2026-11-13 10:00:00.000 ERROR (...)"
    PATTERN_LOGLEVEL_HA,
    # Python default: "2026-11-13 10:00:00,123 - name - ERROR - message"
    re.compile(r" - (?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) - "),
    # logfmt: "level=error msg=..."
    re.compile(r"(?:^|\s)level=\"?(?P<level>[A-Za-z]+)\b"),
    # bracketed time then level: "[2026-11-13 10:00:00] warn: ..."
    re.compile(r"^\[[^\]]{8,40}\] (?P<level>[A-Za-z]+):"),
    # level first, upper case only: "WARNING: ..."
    re.compile(r"^(?P<level>DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|FATAL)\b"),
)

# Colour codes, including combined ones such as bold red "\x1b[1;31m".
ANSI_COLOR_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
REDACT_REPLACEMENT = "***"


# -- configuration ------------------------------------------------------------
@dataclass
class Config:
    host: str
    port: int
    proto: str
    use_ssl: bool
    ssl_verify: bool
    fmt: str
    hostname: str
    redact: list = field(default_factory=list)
    backlog_max_hours: float = 6
    state_dir: str = "/data"
    state_flush_seconds: float = 5
    retry_max_seconds: float = 60
    journal_path: str = "/var/log/journal"

    @property
    def stream(self) -> bool:
        return self.proto == "tcp"


def load_config(env=os.environ) -> Config:
    """Read the configuration; raise ValueError on an unusable value."""
    proto = env.get("SYSLOG_PROTO", "udp").lower()
    if proto not in ("udp", "tcp"):
        raise ValueError(f"unknown protocol {proto!r}")
    fmt = env.get("SYSLOG_FORMAT", "rfc3164").lower()
    if fmt not in ("rfc3164", "rfc5424"):
        raise ValueError(f"unknown syslog format {fmt!r}")
    use_ssl = env.get("SYSLOG_SSL", "false") == "true"
    if use_ssl and proto != "tcp":
        raise ValueError("TLS is only supported with tcp")
    redact = []
    # one regular expression per line; an invalid one stops the app instead
    # of sending unmasked messages (from Milas22)
    for pattern in env.get("REDACT_PATTERNS", "").splitlines():
        if pattern:
            try:
                redact.append(re.compile(pattern))
            except re.error as err:
                raise ValueError(f"invalid redact pattern {pattern!r}: {err}") from err
    return Config(
        host=env["SYSLOG_HOST"],
        port=int(env["SYSLOG_PORT"]),
        proto=proto,
        use_ssl=use_ssl,
        ssl_verify=env.get("SYSLOG_SSL_VERIFY", "true") == "true",
        fmt=fmt,
        hostname=env.get("HAOS_HOSTNAME") or socket.gethostname(),
        redact=redact,
        backlog_max_hours=float(env.get("BACKLOG_MAX_HOURS", "6")),
        state_dir=env.get("STATE_DIR", "/data"),
        state_flush_seconds=float(env.get("STATE_FLUSH_SECONDS", "5")),
        retry_max_seconds=float(env.get("RETRY_MAX_SECONDS", "60")),
        journal_path=env.get("JOURNAL_PATH", "/var/log/journal"),
    )


def log(level: str, message: str) -> None:
    """Own messages, on stdout: they reach the journal and the server too."""
    print(f"{level}: {message}", flush=True)


# -- journal fields -----------------------------------------------------------
def normalize_field(value) -> str:
    """
    Ensure the journal field is a str
    python-systemd returns bytes for non UTF-8 data and a list for repeated fields
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(normalize_field(item) for item in value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def to_datetime(value) -> datetime | None:
    """Journal timestamp (datetime from python-systemd, or microseconds)."""
    if isinstance(value, datetime):
        # python-systemd returns naive local time
        return value if value.tzinfo else value.astimezone()
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value) / 1_000_000, tz=timezone.utc)
    return None


def entry_time(entry: dict) -> datetime:
    """Time of the event: source time if given, else journal reception time."""
    for key in ("_SOURCE_REALTIME_TIMESTAMP", "__REALTIME_TIMESTAMP"):
        moment = to_datetime(entry.get(key))
        if moment is not None:
            return moment
    return datetime.now(timezone.utc)


def level_from(message: str, patterns) -> int | None:
    for pattern in patterns:
        match = pattern.search(message)
        if match:
            severity = LEVEL_TO_SEVERITY.get(match.group("level").upper())
            if severity is not None:
                return severity
    return None


def determine_severity(
    entry: dict, container: str, message: str, last: dict
) -> int:
    if not container:
        priority = entry.get("PRIORITY", DEFAULT_SEVERITY)
        if isinstance(priority, int) and 0 <= priority <= 7:
            return priority
        return DEFAULT_SEVERITY  # invalid client-supplied PRIORITY
    if container in HA_CONTAINERS:
        severity = level_from(message, (PATTERN_LOGLEVEL_HA,))
        if severity is None:  # traceback and continuation lines
            return last.get(container, DEFAULT_SEVERITY)
        last[container] = severity
        return severity
    severity = level_from(message, GENERIC_LEVEL_PATTERNS)
    return DEFAULT_SEVERITY if severity is None else severity


# -- message formats ----------------------------------------------------------
def header_field(value, max_len: int) -> str:
    """Header fields must be printable US-ASCII without spaces or NILVALUE."""
    if value is None:
        return NILVALUE
    value = re.sub(r"[^\x21-\x7e]", "", str(value))[:max_len]
    return value or NILVALUE


def format_rfc5424(severity, moment, hostname, app, procid, message) -> str:
    stamp = moment.astimezone().isoformat(timespec="microseconds")
    # UTF-8 encoded MSG must start with a BOM, pure ASCII is sent as MSG-ANY
    bom = "" if message.isascii() else BOM
    return (
        f"<{FACILITY_USER * 8 + severity}>1 {stamp} {header_field(hostname, 255)} "
        f"{header_field(app, 48)} {header_field(procid, 128)} - - {bom}{message}"
    )


def format_rfc3164(severity, moment, hostname, app, procid, message) -> str:
    local = moment.astimezone()
    stamp = f"{local:%b} {local.day:2d} {local:%H:%M:%S}"  # space-padded day
    return (
        f"<{FACILITY_USER * 8 + severity}>{stamp} {header_field(hostname, 255)} "
        f"{header_field(app, 32)}: {message}"
    )


class Formatter:
    """Turn a journal entry into the payload sent to the server."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.render = format_rfc5424 if cfg.fmt == "rfc5424" else format_rfc3164
        self.last_level: dict = {}

    def redact(self, message: str) -> str:
        for pattern in self.cfg.redact:
            message = pattern.sub(REDACT_REPLACEMENT, message)
        return message

    def payload(self, entry: dict) -> bytes:
        message = normalize_field(entry.get("MESSAGE"))
        container = normalize_field(entry.get("CONTAINER_NAME"))
        if container:
            message = ANSI_COLOR_PATTERN.sub("", message)
            app = container
            procid = None  # the journal only knows dockerd's PID
        else:
            app = normalize_field(entry.get("SYSLOG_IDENTIFIER")) or None
            procid = entry.get("_PID")
        message = self.redact(message)
        severity = determine_severity(entry, container, message, self.last_level)
        line = self.render(
            severity, entry_time(entry), self.cfg.hostname, app, procid, message
        )
        return line.encode("utf-8", errors="replace")


# -- transport ----------------------------------------------------------------
def configure_stream_socket(sock: socket.socket) -> None:
    """Detect dead peers quickly and never block forever."""
    sock.settimeout(SOCKET_TIMEOUT)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for option, value in (
        ("TCP_KEEPIDLE", TCP_KEEPALIVE_IDLE),
        ("TCP_KEEPINTVL", TCP_KEEPALIVE_INTERVAL),
        ("TCP_KEEPCNT", TCP_KEEPALIVE_COUNT),
        ("TCP_USER_TIMEOUT", TCP_USER_TIMEOUT),
    ):
        if hasattr(socket, option):  # Linux-specific options
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)
            except OSError:
                pass


class Transport:
    """One connection to the server; send() raises OSError on failure."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.sock = None
        self.ssl_context = None
        if cfg.use_ssl:
            self.ssl_context = ssl.create_default_context()
            if not cfg.ssl_verify:
                self.ssl_context.check_hostname = False
                self.ssl_context.verify_mode = ssl.CERT_NONE

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def connect(self) -> None:
        socktype = socket.SOCK_STREAM if self.cfg.stream else socket.SOCK_DGRAM
        last_error = OSError("getaddrinfo returned no address")
        for family, kind, proto, _, address in socket.getaddrinfo(
            self.cfg.host, self.cfg.port, 0, socktype
        ):
            sock = socket.socket(family, kind, proto)
            try:
                if self.cfg.stream:
                    configure_stream_socket(sock)
                # UDP too: errors such as "port unreachable" become visible
                sock.connect(address)
                if self.ssl_context is not None:
                    sock = self.ssl_context.wrap_socket(
                        sock, server_hostname=self.cfg.host
                    )
                self.sock = sock
                return
            except OSError as err:  # ssl.SSLError is an OSError
                sock.close()
                last_error = err
        raise last_error

    def peer_closed(self) -> bool:
        """
        A syslog server never sends data: a readable stream socket means the
        server closed the connection (or reset it). Checking before each send
        avoids writing an entry into a connection that is already gone.
        """
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
            if not readable:
                return False
            return self.sock.recv(1) == b""
        except (OSError, ValueError):
            return True

    def send(self, payload: bytes) -> None:
        if self.sock is not None and self.cfg.stream and self.peer_closed():
            self.close()
        if self.sock is None:
            self.connect()
        try:
            if self.cfg.stream:
                # octet counting (RFC 6587), for both formats
                self.sock.sendall(b"%d %b" % (len(payload), payload))
            else:
                self.sock.send(payload)
        except OSError:
            self.close()
            raise


# -- saved position -----------------------------------------------------------
class Position:
    """Cursor of the last entry handed to the server, saved in STATE_DIR."""

    def __init__(self, cfg: Config) -> None:
        self.path = os.path.join(cfg.state_dir, STATE_FILE)
        self.interval = cfg.state_flush_seconds
        self.cursor = None
        self.realtime_usec = None
        self.dirty = False
        self.last_write = 0.0
        self.write_failed = False

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                saved = json.load(handle)
            if isinstance(saved.get("cursor"), str) and isinstance(
                saved.get("realtime_usec"), int
            ):
                return saved
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def advance(self, entry: dict) -> None:
        cursor = entry.get("__CURSOR")
        if not cursor:
            return
        self.cursor = cursor
        moment = to_datetime(entry.get("__REALTIME_TIMESTAMP")) or datetime.now(
            timezone.utc
        )
        self.realtime_usec = int(moment.timestamp() * 1_000_000)
        self.dirty = True

    def flush(self, force: bool = False) -> None:
        if not self.dirty:
            return
        now = time.monotonic()
        if not force and now - self.last_write < self.interval:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {"cursor": self.cursor, "realtime_usec": self.realtime_usec}, handle
                )
            os.replace(tmp, self.path)
            self.dirty = False
            self.last_write = now
            if self.write_failed:
                log("INFO", "journal position saved again")
                self.write_failed = False
        except OSError as err:
            if not self.write_failed:
                log("WARNING", f"cannot save the journal position ({err})")
                self.write_failed = True


def open_journal(cfg: Config, position: Position):
    """
    Place the reader after the last entry sent. Return the reader and the
    entries already read while positioning that still have to be sent.
    """
    jr = journal.Reader(path=cfg.journal_path)
    saved = position.load()
    if saved is None or cfg.backlog_max_hours <= 0:
        jr.seek_tail()
        jr.get_previous()
        log("INFO", "no saved position: starting at the end of the journal")
        return jr, []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.backlog_max_hours)
    cutoff_usec = int(cutoff.timestamp() * 1_000_000)
    if saved["realtime_usec"] < cutoff_usec:
        jr.seek_realtime(cutoff_usec)
        log(
            "WARNING",
            f"saved position older than {cfg.backlog_max_hours:g} h: "
            f"entries before {cutoff.astimezone():%Y-%m-%d %H:%M:%S} are skipped",
        )
        return jr, []

    try:
        jr.seek_cursor(saved["cursor"])
    except (OSError, ValueError):
        jr.seek_realtime(saved["realtime_usec"])
        log("WARNING", "saved cursor unusable: resuming by time, lines may repeat")
        return jr, []
    entry = jr.get_next()
    resumed_at = datetime.fromtimestamp(
        saved["realtime_usec"] / 1_000_000, tz=timezone.utc
    )
    log("INFO", f"resuming after {resumed_at.astimezone():%Y-%m-%d %H:%M:%S}")
    if entry and not jr.test_cursor(saved["cursor"]):
        return jr, [entry]  # the saved entry is gone: this one was never sent
    return jr, []


# -- main loop ----------------------------------------------------------------
class Forwarder:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.formatter = Formatter(cfg)
        self.transport = Transport(cfg)
        self.position = Position(cfg)
        self.stopping = False
        self.down_since = None

    def stop(self, *_args) -> None:
        self.stopping = True

    def pause(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def deliver(self, entry: dict) -> bool:
        """Send one entry, retrying until it is sent; False if stopping."""
        payload = self.formatter.payload(entry)
        delay = 1.0
        while True:
            try:
                self.transport.send(payload)
            except OSError as err:
                if self.down_since is None:
                    self.down_since = time.monotonic()
                    log(
                        "WARNING",
                        f"cannot send to {self.cfg.host}:{self.cfg.port} ({err}); "
                        "entries stay in the journal until it is reachable",
                    )
                if self.stopping:
                    return False
                self.pause(min(delay, self.cfg.retry_max_seconds))
                delay = min(delay * 2, self.cfg.retry_max_seconds)
                continue
            if self.down_since is not None:
                log(
                    "INFO",
                    f"sending again after {time.monotonic() - self.down_since:.0f} s",
                )
                self.down_since = None
            self.position.advance(entry)
            return True

    def run(self) -> None:
        jr, pending = open_journal(self.cfg, self.position)
        log(
            "INFO",
            f"forwarding to {self.cfg.host}:{self.cfg.port}/{self.cfg.proto} "
            f"({self.cfg.fmt})",
        )
        try:
            for entry in pending:
                if not self.deliver(entry):
                    return
            while not self.stopping:
                for entry in jr:
                    if not self.deliver(entry):
                        return
                    self.position.flush()
                    if self.stopping:
                        return
                self.position.flush()
                jr.wait(1)  # short wait: a stop request is seen quickly
        finally:
            self.position.flush(force=True)
            self.transport.close()


def main() -> int:
    try:
        cfg = load_config()
    except (KeyError, ValueError) as err:
        log("ERROR", f"invalid configuration: {err}")
        return 1
    forwarder = Forwarder(cfg)
    signal.signal(signal.SIGTERM, forwarder.stop)
    signal.signal(signal.SIGINT, forwarder.stop)
    forwarder.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
