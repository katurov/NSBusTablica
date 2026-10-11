"""Offline tests for API access control: trusted networks, named tokens, placeholders, IPv4-mapped IPv6,
proxies, disabled auth, 401 aggregation, hidden client list, auth file merge (incl. the Python 3.10 fallback parser)."""
import json, os, sys, tempfile, shutil, threading, unittest, urllib.request, urllib.error
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import nstupido as N           # noqa: E402
import auth as A               # noqa: E402

GOOD = "q8bX0c2m3VtT4yLrK9sZp1HwEe6uJd7A"       # 32 chars, test only


def make_cfg(**auth_over):
    c = N.load_config(os.path.join(os.path.dirname(HERE), "config.toml"))
    c["auth"] = dict(A.DEFAULT_AUTH, **auth_over)
    return c


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


class Server:
    """Real HTTP server on 127.0.0.1; with trusted_networks not covering 127.0.0.1 we are 'outside'."""

    def __init__(self, **auth_over):
        self.clock = Clock()
        c = make_cfg(**auth_over)
        self.svc = N.Service(c, N.Memory(None), fetch_fn=lambda u: {"name": u, "buses": []},
                             gate=N.Gate(0, 5, 180, clock=self.clock, sleep=lambda d: None), clock=self.clock,
                             sleep=lambda d: None, use_feeders=False)
        self.svc.ready.set()
        handler = type("H", (N.Handler,), {"service": self.svc})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


OUTSIDE = {"trusted_networks": ["192.168.42.0/24"], "tokens": {"friend01": GOOD, "friend02": "CHANGE-ME-02"}}


class HttpAuthTest(unittest.TestCase):
    def test_trusted_lan_needs_no_token(self):
        s = Server(tokens={})                   # default trusted networks include 127.0.0.0/8
        try:
            st, _, body = s.get("/frame?stop=6539")
            self.assertEqual(st, 200)
            self.assertTrue(body.startswith("H|6539|"))
            st, _, body = s.get("/")
            self.assertIn("clients", json.loads(body))            # trusted: client list visible
        finally:
            s.close()

    def test_outside_without_token_gets_bare_401(self):
        s = Server(**OUTSIDE)
        try:
            for path in ("/", "/frame?stop=6539", "/buses?stop=6539", "/debug", "/nope"):
                st, h, body = s.get(path)
                self.assertEqual(st, 401, path)
                self.assertEqual(h.get("WWW-Authenticate"), "Bearer")
                self.assertEqual(body, "unauthorized\n")
            st, _, _ = s.get("/frame?stop=6539", {"Authorization": "Bearer wrong-token-wrong-token"})
            self.assertEqual(st, 401)
            self.assertFalse(s.svc.watch.is_watched("6539") and "6539" not in s.svc.watch.defaults)
            self.assertNotIn("127.0.0.1", getattr(s.svc, "http_clients", {}))   # 401s are not recorded per client
        finally:
            s.close()

    def test_valid_token_header_query_xtoken(self):
        s = Server(**OUTSIDE)
        try:
            for path, hdr in (("/frame?stop=6539", {"Authorization": "Bearer " + GOOD}),
                              ("/frame?stop=6539&token=" + GOOD, {}),
                              ("/frame?stop=6539", {"X-Token": GOOD})):
                st, _, body = s.get(path, hdr)
                self.assertEqual(st, 200, (path, hdr))
                self.assertTrue(body.startswith("H|6539|"))
            self.assertEqual(s.svc.auth.by_name["friend01"], 3)
            last = s.svc.http_clients["127.0.0.1"]["last"]
            self.assertNotIn(GOOD, last)                          # token never logged / shown
        finally:
            s.close()

    def test_change_me_placeholder_rejected(self):
        s = Server(**OUTSIDE)
        try:
            st, _, _ = s.get("/frame?stop=6539", {"Authorization": "Bearer CHANGE-ME-02"})
            self.assertEqual(st, 401)
            self.assertEqual(s.svc.auth.refused, {"friend02": "placeholder"})
            self.assertTrue(any("friend02 ignored" in l for l in s.svc.auth.startup_lines()))
        finally:
            s.close()

    def test_clients_list_hidden_for_token_clients(self):
        s = Server(**OUTSIDE)
        try:
            st, _, body = s.get("/", {"Authorization": "Bearer " + GOOD})
            self.assertEqual(st, 200)
            d = json.loads(body)
            self.assertNotIn("clients", d)
            self.assertIn("queue", d)
        finally:
            s.close()

    def test_disabled_auth_accepts_everyone(self):
        s = Server(enabled=False, trusted_networks=[], tokens={})
        try:
            self.assertEqual(s.get("/frame?stop=6539")[0], 200)
            self.assertIn("DISABLED", s.svc.auth.startup_lines()[0])
        finally:
            s.close()

    def test_no_tokens_rejects_all_outside(self):
        s = Server(trusted_networks=["192.168.42.0/24"], tokens={})
        try:
            self.assertEqual(s.get("/frame?stop=6539", {"Authorization": "Bearer " + GOOD})[0], 401)
            self.assertTrue(any("all rejected" in l for l in s.svc.auth.startup_lines()))
        finally:
            s.close()


class UnitAuthTest(unittest.TestCase):
    def test_ipv4_mapped_ipv6_and_networks(self):
        a = A.Auth(dict(A.DEFAULT_AUTH))
        for peer in ("192.168.42.7", "::ffff:192.168.42.7", "127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertEqual(a.check(a.client_ip(peer), {}, {})[0], "trusted", peer)
        for peer in ("192.168.43.7", "::ffff:8.8.8.8", "2001:db8::1", "10.0.0.1"):
            self.assertEqual(a.check(a.client_ip(peer), {}, {})[0], "denied", peer)

    def test_forwarded_for_only_from_trusted_proxy(self):
        a = A.Auth(dict(A.DEFAULT_AUTH, trusted_networks=["192.168.42.0/24"]))
        self.assertEqual(str(a.client_ip("8.8.8.8", "192.168.42.5")), "8.8.8.8")       # spoofed XFF ignored
        p = A.Auth(dict(A.DEFAULT_AUTH, trusted_networks=["192.168.42.0/24"], trusted_proxies=["10.0.0.1/32"]))
        self.assertEqual(str(p.client_ip("10.0.0.1", "8.8.8.8")), "8.8.8.8")
        self.assertEqual(str(p.client_ip("10.0.0.1", "192.168.42.5, 8.8.8.8")), "8.8.8.8")   # right-most wins
        self.assertEqual(str(p.client_ip("10.0.0.1", "8.8.8.8, 10.0.0.1")), "8.8.8.8")
        self.assertEqual(str(p.client_ip("10.0.0.1", None)), "10.0.0.1")

    def test_tokens_validated_and_first_use_reported_once(self):
        a = A.Auth({"tokens": {"a": GOOD, "b": "", "c": "short", "d": "change-me-x"}})
        self.assertEqual(set(a.tokens), {"a"})
        self.assertEqual(set(a.refused), {"b", "c", "d"})
        ip = A.parse_ip("8.8.8.8")
        self.assertEqual(a.check(ip, {"Authorization": "bearer " + GOOD}, {}), ("token-first", "a"))
        self.assertEqual(a.check(ip, {"Authorization": "Bearer " + GOOD}, {}), ("token", "a"))
        self.assertIsNone(a.match(GOOD[:-1]))
        self.assertIsNone(a.match(""))

    def test_denied_aggregation(self):
        clock = Clock()
        a = A.Auth({"tokens": {}}, clock=clock)
        self.assertIsNone(a.flush_denied(1800))                   # window not over
        clock.t += 1800
        self.assertIsNone(a.flush_denied(1800))                   # nothing denied -> no line
        for ip in ("8.8.8.8", "8.8.8.8", "1.1.1.1"):
            a.check(A.parse_ip(ip), {}, {})
        self.assertIsNone(a.flush_denied(1800))
        clock.t += 1800
        self.assertEqual(a.flush_denied(1800), "unauthorized: 3 requests in last 30 min from 2 address(es)")
        clock.t += 1800
        self.assertIsNone(a.flush_denied(1800))

    def test_usage_in_stats_line(self):
        s = Server(**OUTSIDE)
        try:
            s.get("/frame?stop=6539", {"Authorization": "Bearer " + GOOD})
            self.assertIn("requests by token: friend01 1", s.svc.stats_line())
            self.assertIn("requests by token: none", s.svc.stats_line())     # reset per stats window
        finally:
            s.close()

    def test_redact(self):
        self.assertEqual(A.redact("/frame?stop=6539&token=abc"), "/frame?stop=6539")
        self.assertEqual(A.redact("/buses?token=abc&stop=1,2"), "/buses?stop=1,2")


class AuthFileTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        shutil.copy(os.path.join(os.path.dirname(HERE), "config.toml"), os.path.join(self.d, "config.toml"))

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self, text):
        with open(os.path.join(self.d, "auth.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_tracked_config_has_no_tokens_and_example_only_placeholders(self):
        root = os.path.dirname(HERE)
        c = N.load_config(os.path.join(self.d, "config.toml"))   # no auth.toml here
        self.assertEqual(c["auth"]["tokens"], {})
        self.assertFalse(c["auth"]["file_loaded"])
        with open(os.path.join(root, "auth.example.toml"), encoding="utf-8") as f:
            ex = N._mini_toml(f.read())
        toks = ex["auth"]["tokens"]
        self.assertEqual(len(toks), 10)
        self.assertTrue(all(v.startswith("CHANGE-ME") for v in toks.values()))
        self.assertEqual(A.Auth({"tokens": toks}).tokens, {})

    def test_table_form_merged(self):
        self.write('[auth.tokens]\nfriend01 = "%s"\nfriend02 = "CHANGE-ME-02"\n' % GOOD)
        c = N.load_config(os.path.join(self.d, "config.toml"))
        self.assertTrue(c["auth"]["file_loaded"])
        self.assertEqual(c["auth"]["tokens"]["friend01"], GOOD)
        self.assertEqual(c["auth"]["trusted_networks"][-1], "192.168.42.0/24")   # from config.toml

    def test_inline_form_and_overrides_with_fallback_parser(self):
        text = '[auth]\ntrusted_networks = ["10.0.0.0/8"]\ntokens = { phone = "%s", "tab-let" = \'%s\' }\n' % (GOOD, GOOD[::-1])
        self.write(text)
        parsed = N._mini_toml(text)                               # the Python 3.10 path
        self.assertEqual(parsed["auth"]["tokens"], {"phone": GOOD, "tab-let": GOOD[::-1]})
        c = N.load_config(os.path.join(self.d, "config.toml"))
        self.assertEqual(c["auth"]["trusted_networks"], ["10.0.0.0/8"])
        self.assertEqual(set(c["auth"]["tokens"]), {"phone", "tab-let"})

    def test_top_level_tokens_table(self):
        self.write('[tokens]\nfriend03 = "%s"\n' % GOOD)
        c = N.load_config(os.path.join(self.d, "config.toml"))
        self.assertEqual(c["auth"]["tokens"], {"friend03": GOOD})


if __name__ == "__main__":
    unittest.main()
