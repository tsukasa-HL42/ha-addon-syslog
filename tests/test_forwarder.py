"""
Offline test bench for journal2syslog.py.

The forwarder runs as a real subprocess against a fake journal (JSON lines,
see fake/systemd/journal.py) and a local syslog receiver. Every test checks
what arrives on the wire, so the same bench runs against any version of the
script: set J2S_SCRIPT to its path (default: ../syslog/journal2syslog.py).

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fake")
SCRIPT = os.environ.get(
    "J2S_SCRIPT", os.path.join(HERE, os.pardir, "syslog", "journal2syslog.py")
)
HOST = "haos-test"

RFC5424 = re.compile(
    r"^<(?P<pri>\d{1,3})>1 (?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) "
    r"(?P<procid>\S+) (?P<msgid>\S+) (?P<sd>-|\[.*?\]) ?(?P<msg>.*)$",
    re.DOTALL,
)


# -- local syslog receiver ----------------------------------------------------
class Receiver:
    """TCP syslog receiver: octet-counted frames, or LF / NUL separated ones."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.lock = threading.Lock()
        self.port = 0
        self._server: socket.socket | None = None
        self._conns: list[socket.socket] = []
        self.start()

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # restarted on the same port: Linux keeps it in TIME_WAIT otherwise
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.port))
        server.listen(5)
        self.port = server.getsockname()[1]
        self._server = server
        threading.Thread(target=self._accept, args=(server,), daemon=True).start()

    def _accept(self, server: socket.socket) -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn: socket.socket) -> None:
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while buf:
                match = re.match(rb"^(\d+) ", buf)
                if match:
                    size = int(match.group(1))
                    start = match.end()
                    if len(buf) < start + size:
                        break
                    frame, buf = buf[start : start + size], buf[start + size :]
                else:
                    cut = min(
                        (i for i in (buf.find(b"\n"), buf.find(b"\0")) if i >= 0),
                        default=-1,
                    )
                    if cut < 0:
                        break
                    frame, buf = buf[:cut], buf[cut + 1 :]
                    if not frame:
                        continue
                with self.lock:
                    self.messages.append(frame.decode("utf-8", "replace"))

    def stop(self) -> None:
        """Close the listener and every connection (the peer sees a FIN)."""
        if self._server is not None:
            self._server.close()
            self._server = None
        for conn in self._conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        self._conns.clear()

    def reset(self) -> None:
        """Abort every connection with a reset: the peer gets an error."""
        linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
        for conn in self._conns:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
            # Linux sends the reset only once the reading thread lets go of
            # the socket: wake it up first, without sending an end of stream
            try:
                conn.shutdown(socket.SHUT_RD)
            except OSError:
                pass
            conn.close()
        self._conns.clear()

    def received(self) -> list[str]:
        with self.lock:
            return list(self.messages)

    def bodies(self) -> list[str]:
        return [parse(m)["msg"] if parse(m) else m for m in self.received()]


class UdpReceiver:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.messages: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                data = self.sock.recv(65536)
            except OSError:
                return
            self.messages.append(data.decode("utf-8", "replace"))

    def stop(self) -> None:
        self.sock.close()


def parse(message: str) -> dict | None:
    match = RFC5424.match(message)
    if not match:
        return None
    fields = match.groupdict()
    fields["msg"] = fields["msg"].lstrip("\ufeff")
    pri = int(fields["pri"])
    fields["facility"], fields["severity"] = divmod(pri, 8)
    return fields


def usec(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000)


# -- fixture ------------------------------------------------------------------
class Bench(unittest.TestCase):
    fmt = "rfc5424"
    proto = "tcp"

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="j2s-")
        self.journal = os.path.join(self.dir, "journal.jsonl")
        self.state = os.path.join(self.dir, "data")
        os.makedirs(self.state)
        open(self.journal, "w", encoding="utf-8").close()
        self.seq = 0
        self.procs: list[subprocess.Popen] = []
        self.logs: list[str] = []
        self.receiver = UdpReceiver() if self.proto == "udp" else Receiver()
        self.extra_env: dict[str, str] = {}

    def tearDown(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
        self.receiver.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    # journal ---------------------------------------------------------------
    def append(self, message: str, when: datetime | None = None, **fields) -> dict:
        self.seq += 1
        entry = {
            "__SEQ": self.seq,
            "__CURSOR": f"s={self.seq}",
            "__REALTIME_TIMESTAMP": usec(when or datetime.now(timezone.utc)),
            "MESSAGE": message,
            "PRIORITY": 6,
            "SYSLOG_IDENTIFIER": "test",
            "_PID": 4242,
        }
        entry.update(fields)
        entry = {key: value for key, value in entry.items() if value is not None}
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return entry

    # forwarder -------------------------------------------------------------
    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": FAKE,
                "PYTHONIOENCODING": "utf-8",
                "FAKE_JOURNAL": self.journal,
                "SYSLOG_HOST": "127.0.0.1",
                "SYSLOG_PORT": str(self.receiver.port),
                "SYSLOG_PROTO": self.proto,
                "SYSLOG_SSL": "false",
                "SYSLOG_SSL_VERIFY": "false",
                "SYSLOG_FORMAT": self.fmt,
                "HAOS_HOSTNAME": HOST,
                "STATE_DIR": self.state,
                "STATE_FLUSH_SECONDS": "0.1",
                "RETRY_MAX_SECONDS": "0.5",
                "REDACT_PATTERNS": "",
                "BACKLOG_MAX_HOURS": "6",
            }
        )
        env.update(self.extra_env)
        return env

    def start(self) -> subprocess.Popen:
        log = os.path.join(self.dir, f"forwarder-{len(self.procs)}.log")
        self.logs.append(log)
        with open(log, "wb") as out:
            proc = subprocess.Popen(
                [sys.executable, SCRIPT],
                env=self.env(),
                cwd=self.dir,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        self.procs.append(proc)
        return proc

    def output(self) -> str:
        text = ""
        for log in self.logs:
            with open(log, encoding="utf-8", errors="replace") as handle:
                text += handle.read()
        return text

    def start_synced(self) -> subprocess.Popen:
        """Start the forwarder and wait until it forwards new entries."""
        proc = self.start()
        deadline = time.monotonic() + 15
        n = 0
        while time.monotonic() < deadline:
            n += 1
            self.append(f"sync-{n}")
            if self.wait_until(lambda: any("sync-" in b for b in self.receiver.bodies()), 0.3):
                return proc
            if proc.poll() is not None:
                break
        self.fail(f"forwarder never forwarded anything:\n{self.output()}")

    def wait_until(self, predicate, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def wait_for_bodies(self, *wanted: str, timeout: float = 10.0) -> None:
        ok = self.wait_until(
            lambda: all(w in self.receiver.bodies() for w in wanted), timeout
        )
        if not ok:
            self.fail(
                f"missing {[w for w in wanted if w not in self.receiver.bodies()]}; "
                f"received {self.receiver.bodies()}\n{self.output()}"
            )

    def message(self, body: str) -> dict:
        self.wait_for_bodies(body)
        for raw in self.receiver.received():
            fields = parse(raw)
            if fields and fields["msg"] == body:
                return fields
        self.fail(f"{body!r} not parsable as RFC 5424: {self.receiver.received()}")


# -- message format ------------------------------------------------------------
class FormatTests(Bench):
    def test_rfc5424_header_host_entry(self):
        """Host entry: user facility, journal priority, HAOS hostname, identifier."""
        self.start_synced()
        self.append("disk almost full", SYSLOG_IDENTIFIER="kernel", PRIORITY=4, _PID=1)
        fields = self.message("disk almost full")
        self.assertEqual(fields["facility"], 1)
        self.assertEqual(fields["severity"], 4)
        self.assertEqual(fields["host"], HOST)
        self.assertEqual(fields["app"], "kernel")
        self.assertEqual(fields["procid"], "1")

    def test_notice_is_kept(self):
        """Journal priority 5 (notice) is sent as notice, not folded into info."""
        self.start_synced()
        self.append("notice line", SYSLOG_IDENTIFIER="systemd", PRIORITY=5)
        self.assertEqual(self.message("notice line")["severity"], 5)

    def test_event_timestamp(self):
        """The syslog timestamp is the time of the journal entry, not of sending."""
        self.start_synced()
        when = datetime.now(timezone.utc).replace(microsecond=123456) - timedelta(minutes=7)
        self.append("old event", when=when)
        sent = datetime.fromisoformat(self.message("old event")["ts"].replace("Z", "+00:00"))
        self.assertLess(abs((sent - when).total_seconds()), 0.001)

    def test_program_from_process_name(self):
        """A daemon without syslog identifier (udisksd on HAOS) gets its process name."""
        self.start_synced()
        self.append(
            "udisks daemon version 2.10.1 starting",
            SYSLOG_IDENTIFIER=None, _COMM="udisksd", _PID=812,
        )
        fields = self.message("udisks daemon version 2.10.1 starting")
        self.assertEqual(fields["app"], "udisksd")
        self.assertEqual(fields["procid"], "812")

    def test_non_ascii_message_has_bom(self):
        """RFC 5424: a UTF-8 message starts with a BOM, an ASCII one does not."""
        self.start_synced()
        self.append("température élevée")
        self.wait_for_bodies("température élevée")
        raw = [m for m in self.receiver.received() if "température" in m][0]
        self.assertIn(" - - ﻿température élevée", raw)
        sync = [m for m in self.receiver.received() if "sync-" in m][0]
        self.assertNotIn("﻿", sync)

    def test_invalid_priority(self):
        """A non-numeric journal PRIORITY is sent as info."""
        self.start_synced()
        self.append("odd priority", PRIORITY="loud")
        self.assertEqual(self.message("odd priority")["severity"], 6)

    def test_composite_ansi_colours_removed(self):
        """Bold and combined colour codes are stripped from container lines."""
        self.start_synced()
        self.append(
            "\x1b[1;31mboom\x1b[0m done",
            CONTAINER_NAME="addon_core_test",
            SYSLOG_IDENTIFIER="addon_core_test",
        )
        self.wait_for_bodies("boom done")

    def test_container_program_and_procid(self):
        """Container lines: program = container name, no meaningless dockerd PID."""
        self.start_synced()
        entry = self.append("from container", CONTAINER_NAME="addon_core_mosquitto", _PID=777)
        del entry  # SYSLOG_IDENTIFIER is "test" here: the container name must win
        fields = self.message("from container")
        self.assertEqual(fields["app"], "addon_core_mosquitto")
        self.assertEqual(fields["procid"], "-")

    def test_core_severity_and_traceback(self):
        """Home Assistant Core: level read in the line, kept for traceback lines."""
        self.start_synced()
        self.append(
            "2026-11-13 10:00:00.000 ERROR (MainThread) [x] failed",
            CONTAINER_NAME="homeassistant",
            SYSLOG_IDENTIFIER="homeassistant",
        )
        self.append("Traceback (most recent call last):", CONTAINER_NAME="homeassistant",
                    SYSLOG_IDENTIFIER="homeassistant")
        self.assertEqual(
            self.message("2026-11-13 10:00:00.000 ERROR (MainThread) [x] failed")["severity"], 3
        )
        self.assertEqual(self.message("Traceback (most recent call last):")["severity"], 3)

    def test_addon_severity(self):
        """Add-on lines: level read from the common formats, info otherwise."""
        self.start_synced()
        cases = {
            "[10:00:00] ERROR: bashio failure": 3,
            "s6-rc: warning: service slow": 4,
            "time=1 level=error msg=broken": 3,
            "[2026-11-13 10:00:00] warn: \tz2m: retry": 4,
            "2026-11-13 10:00:00,123 - app - CRITICAL - dead": 2,
            "WARNING: plain prefix": 4,
            # OpenThread Border Router: uptime, then one-letter level
            "6d.00:27:52.159 [D] P-SpinelDrive-: Received spinel frame": 7,
            "6d.00:27:52.160 [N] Mle-----------: Role detached -> leader": 5,
            "6d.00:27:52.161 [W] Platform------: radio timeout": 4,
            "6d.00:27:52.162 [C] Platform------: init failed": 2,
            # Mosquitto plugins: lower-case level and a colon first
            "error: received null username or password for unpwd check": 3,
            "warning: retained message dropped": 4,
            "Info about nothing": 6,
            "information: not a level": 6,
        }
        for i, body in enumerate(cases):
            self.append(body, CONTAINER_NAME=f"app_core_a{i}", PRIORITY=3)
        for body, severity in cases.items():
            with self.subTest(body=body):
                self.assertEqual(self.message(body)["severity"], severity)


class Rfc3164Tests(Bench):
    fmt = "rfc3164"

    def test_rfc3164_line(self):
        self.start_synced()
        self.append("bsd line", SYSLOG_IDENTIFIER="dropbear", PRIORITY=3)
        self.assertTrue(
            self.wait_until(
                lambda: any(
                    re.match(r"^<11>\w{3} [ \d]\d \d\d:\d\d:\d\d haos-test dropbear: bsd line$", m)
                    for m in self.receiver.received()
                )
            ),
            self.receiver.received(),
        )


class UdpTests(Bench):
    proto = "udp"

    def test_udp_datagram(self):
        self.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.receiver.messages:
            self.append("udp line")
            time.sleep(0.3)
        self.assertTrue(any(m.endswith(" udp line") for m in self.receiver.messages))


    def test_oversized_entry_does_not_block(self):
        """An entry larger than a datagram is cut and the next ones still go."""
        self.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.receiver.messages:
            self.append("udp line")
            time.sleep(0.3)
        self.append("big " + "x" * 70000)
        self.append("after big")
        ok = self.wait_until(
            lambda: any(m.endswith(" after big") for m in self.receiver.messages)
        )
        self.assertTrue(ok, "blocked behind the oversized entry: " + self.output())
        big = [m for m in self.receiver.messages if " big xxx" in m]
        self.assertEqual(len(big), 1)
        self.assertTrue(big[0].endswith(TRUNCATED), big[0][-40:])


# -- message size --------------------------------------------------------------
TRUNCATED = "...[truncated]"


class SizeTests(Bench):
    def setUp(self) -> None:
        super().setUp()
        self.extra_env["MAX_MESSAGE_BYTES"] = "1024"

    def long_one(self) -> str:
        self.wait_until(
            lambda: any("long " in m for m in self.receiver.received())
        )
        found = [m for m in self.receiver.received() if "long " in m]
        self.assertEqual(len(found), 1, self.output())
        return found[0]

    def test_long_message_cut_at_limit(self):
        """A message over the limit is cut to it, marked, and stays valid."""
        self.start_synced()
        # two-byte characters: the cut must not split one
        self.append("long " + "é" * 5000)
        self.append("after long")
        self.wait_for_bodies("after long")
        message = self.long_one()
        self.assertLessEqual(len(message.encode("utf-8")), 1024)
        self.assertGreater(len(message.encode("utf-8")), 1000)
        self.assertTrue(message.endswith(TRUNCATED), message[-40:])
        self.assertNotIn("�", message)
        self.assertEqual(parse(message)["app"], "test")

    def test_message_at_limit_untouched(self):
        self.start_synced()
        self.append("long " + "y" * 300)
        self.assertFalse(self.long_one().endswith(TRUNCATED))

    def test_unusable_limit_stops_before_sending(self):
        self.extra_env["MAX_MESSAGE_BYTES"] = "100"
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 1)
        self.assertIn("MAX_MESSAGE_BYTES", self.output())
        self.assertEqual(self.receiver.received(), [])


# -- secrets -------------------------------------------------------------------
class RedactTests(Bench):
    def test_secrets_masked(self):
        self.extra_env["REDACT_PATTERNS"] = "/private_[A-Za-z0-9]{8,}\n(?<=token=)[^&\\s]+"
        self.start_synced()
        self.append("POST /private_AbCdEf123456 HTTP/1.1 200 token=s3cr3t&x=1")
        self.wait_for_bodies("POST *** HTTP/1.1 200 token=***&x=1")

    def test_invalid_pattern_stops_before_sending(self):
        self.extra_env["REDACT_PATTERNS"] = "("
        self.append("secret before start")
        proc = self.start()
        self.append("secret after start")
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("forwarder kept running with an invalid redact pattern")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.receiver.received(), [])


# The pattern given in DOCS.md for the hourly sweep seen on HAOS (13/11/2026):
# reverse lookups sent without recursion, refused by the upstream resolver.
SWEEP_PATTERN = (
    r'^hassio_dns: \[INFO\] \S+ - \d+ "PTR IN [0-9.]+in-addr\.arpa\. '
    r'udp \d+ false \d+" REFUSED qr,ra '
)
SWEEP_LINE = ('[INFO] 172.30.32.1:41258 - 30575 "PTR IN 254.2.0.192.in-addr.arpa. '
              'udp 42 false 512" REFUSED qr,ra 42 0.000428374s')


class ExcludeTests(Bench):
    def test_sweep_excluded_and_nothing_else(self):
        """Only the lines matching an exclude pattern are left out."""
        self.extra_env["EXCLUDE_PATTERNS"] = SWEEP_PATTERN
        self.start_synced()
        kept = {
            # the same lookup failing for another reason
            '[INFO] 172.30.32.1:58204 - 34477 "PTR IN 1.2.0.192.in-addr.arpa. '
            'udp 40 false 512" SERVFAIL qr 40 4.00107475s': "hassio_dns",
            # refused, but asked with recursion: a real problem
            '[INFO] 172.30.32.1:1 - 2 "PTR IN 9.2.0.192.in-addr.arpa. '
            'udp 40 false 512" REFUSED qr,rd,ra 40 0.0004s': "hassio_dns",
            # refused, another type of query
            '[INFO] 127.0.0.1:57739 - 19900 "NS IN . udp 17 false 512" '
            'REFUSED qr,aa,rd 17 0.000061706s': "hassio_dns",
            # the very same text from another program
            SWEEP_LINE.replace("30575", "30576"): "app_core_other",
        }
        self.append(SWEEP_LINE, CONTAINER_NAME="hassio_dns")
        for body, container in kept.items():
            self.append(body, CONTAINER_NAME=container)
        self.append("after the excluded ones")
        self.wait_for_bodies(*kept, "after the excluded ones")
        self.assertNotIn(SWEEP_LINE, self.receiver.bodies())

    def test_invalid_exclude_pattern_stops_before_sending(self):
        self.extra_env["EXCLUDE_PATTERNS"] = "("
        self.append("before start")
        proc = self.start()
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("forwarder kept running with an invalid exclude pattern")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.receiver.received(), [])


# -- delivery ------------------------------------------------------------------
class DeliveryTests(Bench):
    def test_first_start_does_not_replay_history(self):
        self.append("history before first start")
        self.start_synced()
        self.append("after start")
        self.wait_for_bodies("after start")
        self.assertNotIn("history before first start", self.receiver.bodies())

    def test_no_loss_while_receiver_down(self):
        """Entries written while the receiver is down arrive once it is back."""
        self.start_synced()
        self.receiver.stop()
        time.sleep(0.3)
        self.append("during outage 1")
        time.sleep(1.0)
        self.append("during outage 2")
        time.sleep(1.0)
        self.receiver.start()
        self.append("after outage")
        self.wait_for_bodies("during outage 1", "during outage 2", "after outage", timeout=15)

    def test_outage_logged_once(self):
        """An outage gives one 'cannot send' line and one 'sending again' line."""
        self.start_synced()
        self.receiver.stop()
        for i in range(5):
            self.append(f"lost? {i}")
            time.sleep(0.2)
        time.sleep(1.5)
        self.receiver.start()
        self.wait_for_bodies(*(f"lost? {i}" for i in range(5)), timeout=15)
        self.assertTrue(self.wait_until(lambda: "sending again" in self.output(), 5), self.output())
        self.assertEqual(self.output().count("cannot send"), 1, self.output())

    def test_own_messages_timestamped(self):
        """The app's own lines start with the time, like bashio's."""
        self.start_synced()
        self.assertRegex(
            self.output(), r"(?m)^\[\d{2}:\d{2}:\d{2}\] INFO: forwarding to "
        )

    def test_unresolvable_host_keeps_running(self):
        """A server name that does not resolve is retried, not fatal."""
        self.extra_env["SYSLOG_HOST"] = "absent.invalid"
        proc = self.start()
        self.assertTrue(self.wait_until(lambda: "forwarding to" in self.output()), self.output())
        self.append("nobody listens")
        time.sleep(3)
        self.assertIsNone(proc.poll(), self.output())
        self.assertIn("cannot send", self.output())

    def test_resume_after_restart(self):
        """Entries written while the forwarder is stopped are sent after restart."""
        first = self.start_synced()
        self.append("before stop")
        self.wait_for_bodies("before stop")
        # a hard kill keeps only what was acknowledged and saved: acknowledgements
        # are checked within the 1 s journal wait, the position saved every 0.1 s
        time.sleep(1.5)
        first.kill()
        first.wait(timeout=10)
        self.append("while stopped 1")
        self.append("while stopped 2")
        self.start()
        self.append("after restart")
        self.wait_for_bodies("while stopped 1", "while stopped 2", "after restart", timeout=15)
        self.assertEqual(self.receiver.bodies().count("before stop"), 1)

    @unittest.skipUnless(os.name == "posix", "SIGTERM is a hard kill on Windows")
    def test_clean_stop_does_not_resend(self):
        """
        A stop by SIGTERM (the Supervisor) saves the position: no duplicate.
        On loopback the acknowledgement is immediate, so the wait itself is
        proven by AcknowledgementTests.test_stop_waits_for_acknowledgements.
        """
        first = self.start_synced()
        self.append("just before stop")
        self.wait_for_bodies("just before stop")
        first.terminate()
        self.assertEqual(first.wait(timeout=10), 0)
        self.start()
        self.append("after clean restart")
        self.wait_for_bodies("after clean restart", timeout=15)
        self.assertEqual(self.receiver.bodies().count("just before stop"), 1)

    def test_backlog_is_capped(self):
        """A saved position older than BACKLOG_MAX_HOURS resumes at the cap."""
        now = datetime.now(timezone.utc)
        old = self.append("10 h old", when=now - timedelta(hours=10))
        self.append("8 h old", when=now - timedelta(hours=8))
        self.append("5 h old", when=now - timedelta(hours=5))
        self.append("1 h old", when=now - timedelta(hours=1))
        with open(os.path.join(self.state, "journal_position.json"), "w", encoding="utf-8") as handle:
            json.dump({"cursor": old["__CURSOR"], "realtime_usec": old["__REALTIME_TIMESTAMP"]}, handle)
        self.start()
        self.wait_for_bodies("5 h old", "1 h old")
        self.assertNotIn("8 h old", self.receiver.bodies())
        self.assertNotIn("10 h old", self.receiver.bodies())

    def test_saved_cursor_not_resent(self):
        """Resuming from a saved position does not resend the saved entry."""
        now = datetime.now(timezone.utc)
        sent = self.append("already sent", when=now - timedelta(minutes=5))
        self.append("not yet sent", when=now - timedelta(minutes=4))
        with open(os.path.join(self.state, "journal_position.json"), "w", encoding="utf-8") as handle:
            json.dump({"cursor": sent["__CURSOR"], "realtime_usec": sent["__REALTIME_TIMESTAMP"]}, handle)
        self.start()
        self.wait_for_bodies("not yet sent")
        time.sleep(0.5)
        self.assertNotIn("already sent", self.receiver.bodies())


# -- in-process tests ------------------------------------------------------------
def load_script(test: unittest.TestCase):
    """Import the script under test with the fake systemd, without running it."""
    env = {
        "SYSLOG_HOST": "127.0.0.1", "SYSLOG_PORT": "9", "SYSLOG_PROTO": "tcp",
        "SYSLOG_SSL": "false", "SYSLOG_SSL_VERIFY": "false",
        "SYSLOG_FORMAT": "rfc5424", "HAOS_HOSTNAME": HOST,
        "FAKE_JOURNAL": os.devnull,
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    sys.path.insert(0, FAKE)
    # no __pycache__ next to the script: the syslog folder is copied to HAOS
    saved_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("j2s_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # needed by dataclasses
        test.addCleanup(sys.modules.pop, spec.name, None)
        done = threading.Event()

        def load():
            try:
                spec.loader.exec_module(module)
            finally:
                done.set()

        threading.Thread(target=load, daemon=True).start()
        test.assertTrue(done.wait(5), "importing the script runs the forwarder")
        return module
    finally:
        sys.dont_write_bytecode = saved_bytecode
        sys.path.remove(FAKE)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class SocketOptionTests(unittest.TestCase):
    def test_stream_socket_detects_dead_peer(self):
        """Stream sockets get a timeout and TCP keepalive (dead peer detection)."""
        module = load_script(self)
        self.assertTrue(hasattr(module, "configure_stream_socket"))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            module.configure_stream_socket(sock)
            self.assertEqual(sock.gettimeout(), 10)
            self.assertNotEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 0)
        finally:
            sock.close()

    def test_unacknowledged_bytes_measured(self):
        """Linux: bytes not yet acknowledged by the server are measured."""
        module = load_script(self)
        receiver = Receiver()
        self.addCleanup(receiver.stop)
        cfg = module.Config(
            host="127.0.0.1", port=receiver.port, proto="tcp", use_ssl=False,
            ssl_verify=False, fmt="rfc5424", hostname=HOST,
        )
        transport = module.Transport(cfg)
        self.addCleanup(transport.close)
        transport.send(b"x" * 100)
        self.assertEqual(transport.sent_bytes, len(b"100 ") + 100)
        if sys.platform.startswith("linux"):
            deadline = time.monotonic() + 5
            while transport.unacked() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(transport.unacked(), 0)
        else:
            self.assertIsNone(transport.unacked(), "no SIOCOUTQ outside Linux")

    def _connected_transport(self, module, receiver):
        cfg = module.Config(
            host="127.0.0.1", port=receiver.port, proto="tcp", use_ssl=False,
            ssl_verify=False, fmt="rfc5424", hostname=HOST,
        )
        transport = module.Transport(cfg)
        self.addCleanup(transport.close)
        transport.send(b"first")
        deadline = time.monotonic() + 5
        while not receiver.received() and time.monotonic() < deadline:
            time.sleep(0.05)
        return transport

    def test_failure_reason_closed_by_server(self):
        """The server ends the connection (a restart): said as such."""
        module = load_script(self)
        receiver = Receiver()
        self.addCleanup(receiver.stop)
        transport = self._connected_transport(module, receiver)
        receiver.stop()
        time.sleep(0.3)
        with self.assertRaises(OSError) as caught:
            transport.send(b"second")
        self.assertEqual(str(caught.exception), "connection closed by the server")

    def test_failure_reason_from_the_system(self):
        """
        A connection the system gave up (reset, or unacknowledged data timed
        out) is reported with the system's own error, not as a close.
        """
        module = load_script(self)
        receiver = Receiver()
        self.addCleanup(receiver.stop)
        transport = self._connected_transport(module, receiver)
        receiver.reset()
        time.sleep(0.3)
        with self.assertRaises(OSError) as caught:
            transport.send(b"second")
        self.assertNotEqual(str(caught.exception), "connection closed by the server")
        self.assertIsNotNone(caught.exception.errno, "not the system's error")


class FakeLink:
    """
    Transport stand-in that models TCP acknowledgements: a payload reaches the
    server only once acknowledged. When the server vanishes, what was sent
    but not acknowledged is lost, as in the kernel send buffer.
    """

    def __init__(self) -> None:
        self.generation = 0
        self.sent_bytes = 0
        self.connected = False
        self.vanished = False
        self.failing_connects = 0
        self.unacked_payloads: list = []
        self.received: list = []

    def connect(self) -> None:
        if self.failing_connects:
            self.failing_connects -= 1
            raise TimeoutError("timed out")
        self.connected = True
        self.vanished = False
        self.generation += 1
        self.sent_bytes = 0
        self.unacked_payloads = []

    def send(self, payload: bytes) -> None:
        if self.vanished and self.connected:
            raise TimeoutError("timed out")
        if not self.connected:
            self.connect()
        self.sent_bytes += len(payload)
        self.unacked_payloads.append(payload)

    def unacked(self) -> int:
        return sum(len(p) for p in self.unacked_payloads)

    def acknowledge(self) -> None:
        self.received += self.unacked_payloads
        self.unacked_payloads = []

    def vanish(self, failing_connects: int) -> None:
        """Server gone without a reset; the next connects time out."""
        self.vanished = True
        self.failing_connects = failing_connects

    def close(self) -> None:
        self.connected = False


class AcknowledgementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_script(self)
        self.dir = tempfile.mkdtemp(prefix="j2s-ack-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = self.module.Config(
            host="127.0.0.1", port=9, proto="tcp", use_ssl=False, ssl_verify=False,
            fmt="rfc5424", hostname=HOST, state_dir=self.dir, retry_max_seconds=0.01,
        )
        self.link = FakeLink()
        self.forwarder = self.module.Forwarder(self.cfg, transport=self.link)
        now = datetime.now(timezone.utc)
        self.entries = [
            {
                "MESSAGE": f"entry {i}",
                "PRIORITY": 6,
                "SYSLOG_IDENTIFIER": "test",
                "__CURSOR": f"s={i}",
                "__REALTIME_TIMESTAMP": now + timedelta(milliseconds=i),
            }
            for i in range(10)
        ]

    def received(self) -> list:
        return [parse(p.decode("utf-8"))["msg"] for p in self.link.received]

    def test_unacknowledged_entries_resent_after_dead_peer(self):
        """Entries the server never acknowledged are sent again, none is lost."""
        fwd, link = self.forwarder, self.link
        for entry in self.entries[:4]:
            self.assertTrue(fwd.deliver(entry))
        link.acknowledge()
        fwd.refresh_acks()
        self.assertEqual(fwd.position.cursor, "s=3")

        for entry in self.entries[4:8]:  # handed to the kernel, never acknowledged
            self.assertTrue(fwd.deliver(entry))
        fwd.refresh_acks()
        self.assertEqual(fwd.position.cursor, "s=3", "position moved past unacknowledged entries")

        link.vanish(failing_connects=2)
        self.assertTrue(fwd.deliver(self.entries[8]))
        self.assertTrue(fwd.deliver(self.entries[9]))
        link.acknowledge()
        fwd.refresh_acks()

        received = self.received()
        for i in range(10):
            self.assertIn(f"entry {i}", received)
        self.assertEqual(received.count("entry 4"), 1, received)
        self.assertEqual(fwd.position.cursor, "s=9")

    def test_resent_entries_counted(self):
        """The return of the server says how many entries were sent again."""
        fwd, link = self.forwarder, self.link
        messages = []
        self.module.log = lambda level, message: messages.append(message)
        for entry in self.entries[:4]:
            fwd.deliver(entry)
        link.acknowledge()
        fwd.refresh_acks()
        for entry in self.entries[4:8]:  # never acknowledged
            fwd.deliver(entry)
        link.vanish(failing_connects=1)
        fwd.deliver(self.entries[8])
        again = [m for m in messages if m.startswith("sending again")]
        self.assertEqual(len(again), 1, messages)
        self.assertIn("4 entries not acknowledged, sent again", again[0])

    def test_nothing_resent_nothing_said(self):
        """An outage with nothing in flight keeps the short message."""
        fwd, link = self.forwarder, self.link
        messages = []
        self.module.log = lambda level, message: messages.append(message)
        fwd.deliver(self.entries[0])
        link.acknowledge()
        fwd.refresh_acks()
        link.vanish(failing_connects=1)
        fwd.deliver(self.entries[1])
        again = [m for m in messages if m.startswith("sending again")]
        self.assertEqual(len(again), 1, messages)
        self.assertNotIn("sent again", again[0].replace("sending again", ""))

    def test_stop_waits_for_acknowledgements(self):
        """On stop, entries acknowledged shortly after still move the position."""
        fwd, link = self.forwarder, self.link
        fwd.deliver(self.entries[0])
        self.assertIsNone(fwd.position.cursor)
        timer = threading.Timer(0.3, link.acknowledge)
        timer.start()
        self.addCleanup(timer.cancel)
        fwd.drain(timeout=2)
        self.assertEqual(fwd.position.cursor, "s=0")

    def _excluding(self, report_seconds=3600):
        """Forwarder whose formatter leaves out messages starting with 'drop'."""
        self.cfg.exclude = [re.compile(r"^test: drop")]
        self.cfg.exclude_report_seconds = report_seconds
        return self.module.Forwarder(self.cfg, transport=self.link)

    def _entry(self, i, message):
        entry = dict(self.entries[i])
        entry["MESSAGE"] = message
        return entry

    def test_excluded_entry_waits_for_earlier_acknowledgements(self):
        """An excluded entry never moves the position past unacknowledged ones."""
        fwd, link = self._excluding(), self.link
        fwd.deliver(self.entries[0])                # sent, not acknowledged
        fwd.deliver(self._entry(1, "drop me"))      # left out
        fwd.refresh_acks()
        self.assertIsNone(fwd.position.cursor)
        link.acknowledge()
        fwd.refresh_acks()
        self.assertEqual(fwd.position.cursor, "s=1")
        self.assertEqual(self.received(), ["entry 0"])

    def test_excluded_entry_alone_moves_the_position(self):
        """With nothing in flight, an excluded entry is done at once."""
        fwd = self._excluding()
        fwd.deliver(self._entry(0, "drop me"))
        self.assertEqual(fwd.position.cursor, "s=0")
        self.assertEqual(self.link.sent_bytes, 0, "an excluded entry was sent")

    def test_excluded_entries_not_sent_after_outage(self):
        """After an outage, excluded entries are not sent and the position goes on."""
        fwd, link = self._excluding(), self.link
        fwd.deliver(self.entries[0])                # not acknowledged
        fwd.deliver(self._entry(1, "drop me"))
        link.vanish(failing_connects=1)
        fwd.deliver(self.entries[2])                # outage, then reconnection
        link.acknowledge()
        fwd.refresh_acks()
        self.assertEqual(self.received(), ["entry 0", "entry 2"])
        self.assertEqual(fwd.position.cursor, "s=2")
        self.assertFalse(fwd.inflight)

    def test_excluded_entries_reported(self):
        """The app says how many entries it left out, at most once per period."""
        messages = []
        self.module.log = lambda level, message: messages.append(message)
        fwd = self._excluding(report_seconds=0)
        fwd.deliver(self._entry(0, "drop me"))
        fwd.deliver(self._entry(1, "drop me too"))
        reports = [m for m in messages if m.startswith("excluded")]
        self.assertTrue(reports, messages)
        self.assertRegex(reports[-1], r"^excluded 1 entr(y|ies) ")
        fwd = self._excluding(report_seconds=3600)
        messages.clear()
        fwd.deliver(self._entry(2, "drop me"))
        self.assertEqual([m for m in messages if m.startswith("excluded")], [])

    def test_lone_burst_reported_without_another_exclusion(self):
        """
        A burst left out, then nothing: the count is said once the period is
        over, without waiting for the next entry left out (an hourly sweep
        was only reported by the first entry of the following one).
        """
        messages = []
        self.module.log = lambda level, message: messages.append(message)
        fwd = self._excluding(report_seconds=0.2)
        for index in range(3):
            fwd.deliver(self._entry(index, "drop me"))
        self.assertEqual([m for m in messages if m.startswith("excluded")], [])
        time.sleep(0.3)
        fwd.report_excluded()
        reports = [m for m in messages if m.startswith("excluded")]
        self.assertEqual(len(reports), 1, messages)
        self.assertRegex(reports[0], r"^excluded 3 entries ")
        fwd.report_excluded()
        self.assertEqual(len([m for m in messages if m.startswith("excluded")]), 1)

    def test_pending_count_said_on_stop(self):
        """Entries left out since the last report are not lost on a stop."""
        messages = []
        self.module.log = lambda level, message: messages.append(message)
        fwd = self._excluding(report_seconds=3600)
        fwd.deliver(self._entry(0, "drop me"))
        fwd.report_excluded(force=True)
        reports = [m for m in messages if m.startswith("excluded")]
        self.assertEqual(len(reports), 1, messages)
        self.assertRegex(reports[0], r"^excluded 1 entry ")

    def test_resent_in_journal_order(self):
        """After a dead peer, waiting entries are sent again in their order."""
        fwd, link = self.forwarder, self.link
        for entry in self.entries[:5]:
            fwd.deliver(entry)
        link.vanish(failing_connects=1)
        for entry in self.entries[5:]:
            fwd.deliver(entry)
        link.acknowledge()
        self.assertEqual(self.received(), [f"entry {i}" for i in range(10)])


if __name__ == "__main__":
    unittest.main()
