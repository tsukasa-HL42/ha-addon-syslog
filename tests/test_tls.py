"""
TLS transport against a local TLS server.

The certificate is made on the spot with the openssl command, so that no
private key is kept in the repository; without openssl the tests are skipped.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

from test_forwarder import HOST, load_script


class TlsReceiver:
    """Accepts TLS connections of one protocol version and keeps what it reads."""

    def __init__(self, certfile: str, keyfile: str, version) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certfile, keyfile)
        self.context.minimum_version = version
        self.context.maximum_version = version
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        self.data = b""
        self.conns: list[ssl.SSLSocket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                raw, _ = self.server.accept()
                conn = self.context.wrap_socket(raw, server_side=True)
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn: ssl.SSLSocket) -> None:
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            self.data += chunk

    def close_connections(self) -> None:
        """End every connection properly (the peer sees a TLS close)."""
        for conn in self.conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        self.conns.clear()

    def stop(self) -> None:
        self.server.close()
        self.close_connections()


class TlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("openssl command not found")
        cls.dir = tempfile.mkdtemp(prefix="j2s-tls-")
        cls.cert = os.path.join(cls.dir, "cert.pem")
        cls.key = os.path.join(cls.dir, "key.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "2", "-subj", "/CN=localhost",
             "-keyout", cls.key, "-out", cls.cert],
            check=True, capture_output=True,
            # Git for Windows would turn "/CN=..." into a path
            env=dict(os.environ, MSYS_NO_PATHCONV="1", MSYS2_ARG_CONV_EXCL="*"),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.dir, ignore_errors=True)

    def transport(self, version):
        module = load_script(self)
        receiver = TlsReceiver(self.cert, self.key, version)
        self.addCleanup(receiver.stop)
        cfg = module.Config(
            host="127.0.0.1", port=receiver.port, proto="tcp", use_ssl=True,
            ssl_verify=False, fmt="rfc5424", hostname=HOST,
        )
        transport = module.Transport(cfg)
        self.addCleanup(transport.close)
        return transport, receiver

    def send_three(self, version) -> None:
        transport, receiver = self.transport(version)
        for index in range(3):
            started = time.monotonic()
            transport.send(b"entry %d" % index)
            self.assertLess(
                time.monotonic() - started, 2, f"send {index} waited"
            )
            # lets what the server sent after the handshake arrive
            time.sleep(0.3)
        deadline = time.monotonic() + 5
        wanted = b"7 entry 07 entry 17 entry 2"
        while receiver.data != wanted and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(receiver.data, wanted)

    def test_tls12_sends(self):
        self.send_three(ssl.TLSVersion.TLSv1_2)

    def test_tls13_sends(self):
        """
        TLS 1.3 servers send session tickets after the handshake: the socket
        becomes readable although the connection is fine and holds no data.
        """
        self.send_three(ssl.TLSVersion.TLSv1_3)

    def closed_by_server(self, version) -> None:
        transport, receiver = self.transport(version)
        transport.send(b"first")
        deadline = time.monotonic() + 5
        while not receiver.data and time.monotonic() < deadline:
            time.sleep(0.05)
        receiver.close_connections()
        time.sleep(0.3)
        started = time.monotonic()
        with self.assertRaises(OSError) as caught:
            transport.send(b"second")
        self.assertLess(time.monotonic() - started, 2, "the check waited")
        self.assertEqual(str(caught.exception), "connection closed by the server")

    def test_tls12_closed_by_server(self):
        self.closed_by_server(ssl.TLSVersion.TLSv1_2)

    def test_tls13_closed_by_server(self):
        self.closed_by_server(ssl.TLSVersion.TLSv1_3)


if __name__ == "__main__":
    unittest.main()
