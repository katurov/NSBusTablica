"""Offline tests: scanner noise (connection resets, timeouts, malformed requests) is counted, not printed."""
import errno, os, socket, ssl, sys, threading, time, unittest, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import nstupido as N           # noqa: E402


def make_service():
    c = N.load_config(os.path.join(os.path.dirname(HERE), "config.toml"))
    svc = N.Service(c, N.Memory(None), fetch_fn=lambda u: {"name": u, "buses": []},
                    gate=N.Gate(0, 5, 180, sleep=lambda d: None), sleep=lambda d: None, use_feeders=False)
    svc.ready.set()
    return svc


class ClassifyTest(unittest.TestCase):
    def test_connection_errors(self):
        for e in (ConnectionResetError(104, "reset"), BrokenPipeError(), ConnectionAbortedError(), TimeoutError(),
                  socket.timeout(), ssl.SSLError(), OSError(errno.ECONNRESET, "x"), OSError(errno.EPIPE, "x"),
                  OSError(errno.ETIMEDOUT, "x")):
            self.assertTrue(N.is_connection_error(e), repr(e))
        for e in (ValueError("bug"), KeyError("k"), OSError(errno.ENOENT, "x"), ZeroDivisionError()):
            self.assertFalse(N.is_connection_error(e), repr(e))


class ConnLogTest(unittest.TestCase):
    def test_aggregate_only_when_nonzero(self):
        t = [0.0]
        c = N.ConnLog(1800, clock=lambda: t[0])
        t[0] = 1800
        self.assertEqual(c.flush(), [])
        for ip in ("1.1.1.1", "1.1.1.1", "2.2.2.2"):
            c.drop(ip)
        c.bad_request("3.3.3.3")
        self.assertEqual(c.flush(), [])                     # window not over yet
        t[0] = 3600
        self.assertEqual(c.flush(), ["dropped: 3 connections in last 30 min from 2 address(es)",
                                     "bad requests: 1 in last 30 min from 1 address(es)"])
        t[0] = 5400
        self.assertEqual(c.flush(), [])


class HandleErrorTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.srv = N.Server(("127.0.0.1", 0), N.Handler, bind_and_activate=False)
        self.srv.service = self.svc

    def tearDown(self):
        self.srv.server_close()

    def test_reset_counted_silently(self):
        with self.assertNoLogs("nstupido", "INFO") if hasattr(self, "assertNoLogs") else _null():
            N.LOG.propagate = True
            try:
                try:
                    raise ConnectionResetError(104, "Connection reset by peer")
                except ConnectionResetError:
                    self.srv.handle_error(None, ("9.9.9.9", 5555))
            finally:
                N.LOG.propagate = False
        self.assertEqual(self.svc.conn.dropped["9.9.9.9"], 1)

    def test_other_errors_logged_with_traceback(self):
        N.LOG.propagate = True
        try:
            with self.assertLogs("nstupido", "ERROR") as lg:
                try:
                    raise ValueError("real bug")
                except ValueError:
                    self.srv.handle_error(None, ("9.9.9.9", 5555))
        finally:
            N.LOG.propagate = False
        self.assertIn("Traceback", lg.output[0])
        self.assertIn("ValueError: real bug", lg.output[0])
        self.assertEqual(sum(self.svc.conn.dropped.values()), 0)


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class LiveSocketTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        handler = type("H", (N.Handler,), {"service": self.svc, "timeout": 0.5})
        self.srv = N.Server(("127.0.0.1", 0), handler)
        self.srv.service = self.svc
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addr = self.srv.server_address

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def wait(self, cond):
        for _ in range(100):
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_tls_hello_to_plain_http_is_a_bad_request(self):
        s = socket.create_connection(self.addr)
        s.sendall(b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + b"\x00" * 40 + b"\r\n\r\n")
        s.recv(1024)
        s.close()
        self.assertTrue(self.wait(lambda: sum(self.svc.conn.bad.values()) == 1))

    def test_idle_connection_closed_and_counted(self):
        s = socket.create_connection(self.addr)
        s.sendall(b"GET /frame?stop=6539 HTTP/1.1\r\n")      # never finishes the headers
        self.assertTrue(self.wait(lambda: sum(self.svc.conn.dropped.values()) == 1))
        s.close()

    def test_normal_request_unaffected(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/frame?stop=6539" % self.addr[1], timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.read().startswith(b"H|6539|"))
        self.assertEqual((sum(self.svc.conn.dropped.values()), sum(self.svc.conn.bad.values())), (0, 0))


if __name__ == "__main__":
    unittest.main()
