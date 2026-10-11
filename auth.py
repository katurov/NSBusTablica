"""Who may use the NStupido HTTP API.

Trusted networks (LAN, localhost) need nothing. Everyone else must present a named token:
  Authorization: Bearer <token>   |   X-Token: <token>   |   ?token=<token>
Tokens live in an untracked file (auth.toml next to config.toml, git-ignored) so `git pull` on the server never
touches them. Placeholders ("CHANGE-ME…"), empty and short (< 16 chars) tokens are refused.
X-Forwarded-For is honoured only when the direct peer is in `trusted_proxies`. Stdlib only."""
import collections, hmac, ipaddress, threading, time, urllib.parse

DEFAULT_AUTH = {"enabled": True, "trusted_networks": ["127.0.0.0/8", "::1/128", "192.168.42.0/24"],
                "trusted_proxies": [], "file": "auth.toml", "tokens": {}}
PLACEHOLDER = "CHANGE-ME"
MIN_TOKEN_LEN = 16


def parse_ip(s):
    """'192.168.42.5', '::ffff:192.168.42.5', '[::1]' -> ip_address (IPv4-mapped IPv6 unwrapped) or None."""
    try:
        ip = ipaddress.ip_address(str(s).strip().strip("[]").split("%")[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def parse_networks(items):
    out = []
    for n in items or []:
        try:
            out.append(ipaddress.ip_network(str(n).strip(), strict=False))
        except ValueError:
            pass
    return out


def in_networks(ip, nets):
    return ip is not None and any(ip.version == n.version and ip in n for n in nets)


class Auth:
    def __init__(self, cfg=None, clock=time.time):
        c = dict(DEFAULT_AUTH)
        c.update(cfg or {})
        self.enabled = bool(c.get("enabled", True))
        self.trusted = parse_networks(c.get("trusted_networks"))
        self.proxies = parse_networks(c.get("trusted_proxies"))
        self.tokens, self.refused = {}, {}          # name -> token ; name -> reason
        for name, tok in (c.get("tokens") or {}).items():
            tok = "" if tok is None else str(tok)
            if tok.upper().startswith(PLACEHOLDER):
                self.refused[name] = "placeholder"
            elif len(tok.strip()) < MIN_TOKEN_LEN:
                self.refused[name] = "too short (< %d chars)" % MIN_TOKEN_LEN
            else:
                self.tokens[str(name)] = tok.strip()
        self.clock = clock
        self.lock = threading.Lock()
        self.seen_names = set()
        self.by_name = collections.Counter()        # requests per token name since the last stats line
        self.denied = collections.Counter()         # 401s per address since the last flush
        self.window_start = clock()

    def startup_lines(self):
        if not self.enabled:
            return ["auth: DISABLED ([auth] enabled = false) - every client is accepted"]
        out = ["auth: trusted networks %s; %d token(s) active%s" % (
            ", ".join(map(str, self.trusted)) or "none", len(self.tokens),
            ("; trusted proxies %s" % ", ".join(map(str, self.proxies))) if self.proxies else "")]
        for name, why in sorted(self.refused.items()):
            out.append("auth: WARNING token %s ignored: %s" % (name, why))
        if not self.tokens:
            out.append("auth: no usable tokens - requests from outside the trusted networks are all rejected")
        return out

    def client_ip(self, peer, xff=None):
        """Address to judge: the peer, or - only if the peer is a trusted proxy - the right-most X-Forwarded-For
        entry that is not itself a trusted proxy."""
        ip = parse_ip(peer)
        if xff and in_networks(ip, self.proxies):
            for part in reversed([p for p in xff.split(",") if p.strip()]):
                cand = parse_ip(part)
                if cand is None:
                    break
                ip = cand
                if not in_networks(cand, self.proxies):
                    break
        return ip

    @staticmethod
    def presented(headers, query):
        h = (headers.get("Authorization") or "").strip()
        if h[:7].lower() == "bearer ":
            return h[7:].strip()
        if headers.get("X-Token"):
            return headers.get("X-Token").strip()
        q = query.get("token") if isinstance(query, dict) else None
        return q[-1].strip() if q else None

    def match(self, token):
        """Constant-time comparison against every configured token -> name or None."""
        if not token:
            return None
        found = None
        tb = token.encode("utf-8")
        for name, t in self.tokens.items():
            if hmac.compare_digest(tb, t.encode("utf-8")):
                found = name
        return found

    def check(self, ip, headers, query):
        """-> ("trusted", None) | ("token", name) | ("denied", None). Counts usage / denials."""
        if not self.enabled or in_networks(ip, self.trusted):
            return "trusted", None
        name = self.match(self.presented(headers, query))
        with self.lock:
            if name is None:
                self.denied[str(ip)] += 1
                return "denied", None
            self.by_name[name] += 1
            first = name not in self.seen_names
            self.seen_names.add(name)
        return ("token", name) if not first else ("token-first", name)

    def flush_denied(self, every_s, now=None):
        """At most one line per window, only if there were rejected requests."""
        now = self.clock() if now is None else now
        with self.lock:
            if now - self.window_start < every_s:
                return None
            total, addrs = sum(self.denied.values()), len(self.denied)
            self.denied.clear()
            mins = round((now - self.window_start) / 60)
            self.window_start = now
        if not total:
            return None
        return "unauthorized: %d requests in last %d min from %d address(es)" % (total, mins, addrs)

    def usage_line(self):
        with self.lock:
            if not self.by_name:
                return "requests by token: none"
            s = ", ".join("%s %d" % kv for kv in sorted(self.by_name.items(), key=lambda kv: (-kv[1], kv[0])))
            self.by_name.clear()
        return "requests by token: " + s


def redact(path):
    """Drop ?token=… from a path before it is logged or shown."""
    u = urllib.parse.urlsplit(path or "")
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k != "token"]
    return urllib.parse.urlunsplit(("", "", u.path, urllib.parse.urlencode(q, safe=","), ""))
