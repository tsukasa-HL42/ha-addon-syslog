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
            "Info about nothing": 6,
        }
        for i, body in enumerate(cases):
            self.append(body, CONTAINER_NAME=f"addon_core_a{i}", PRIORITY=3)
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
        time.sleep(0.5)  # let the position be saved
        first.kill()
        first.wait(timeout=10)
        self.append("while stopped 1")
        self.append("while stopped 2")
        self.start()
        self.append("after restart")
        self.wait_for_bodies("while stopped 1", "while stopped 2", "after restart", timeout=15)
        self.assertEqual(self.receiver.bodies().count("before stop"), 1)

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


# -- socket options --------------------------------------------------------------
class SocketOptionTests(unittest.TestCase):
    def test_stream_socket_detects_dead_peer(self):
        """Stream sockets get a timeout and TCP keepalive (dead peer detection)."""
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
        self.addCleanup(setattr, sys, "dont_write_bytecode", sys.dont_write_bytecode)
        sys.dont_write_bytecode = True
        try:
            spec = importlib.util.spec_from_file_location("j2s_under_test", SCRIPT)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module  # needed by dataclasses
            self.addCleanup(sys.modules.pop, spec.name, None)
            done = threading.Event()

            def load():
                try:
                    spec.loader.exec_module(module)
                finally:
                    done.set()

            threading.Thread(target=load, daemon=True).start()
            self.assertTrue(done.wait(5), "importing the script runs the forwarder")
            self.assertTrue(hasattr(module, "configure_stream_socket"))
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                module.configure_stream_socket(sock)
                self.assertEqual(sock.gettimeout(), 10)
                self.assertNotEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 0)
            finally:
                sock.close()
        finally:
            sys.path.remove(FAKE)
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
