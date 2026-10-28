"""The deployment files behind SWAG (deploy/swag): the fail2ban filter and the masking of the
access log, with Python's re (fail2ban's own engine; nginx's PCRE patterns differ only in the
spelling of named groups). nginx itself is checked by hand (docs/deployment.md, `nginx -t`)."""

import configparser
import os
import re

SWAG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "swag")
HOST = r"(?P<host>[0-9A-Fa-f:.]+|[\w\-.^_]*\w)"  # what fail2ban puts for <HOST> (simplified)


def _filter():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(os.path.join(SWAG, "fail2ban", "findaline.conf"), encoding="utf-8")
    return [re.compile(line.strip().replace("<HOST>", HOST))
            for line in parser["Definition"]["failregex"].splitlines() if line.strip()]


def _line(status, request='GET /api/jobs HTTP/2.0', ip="203.0.113.7", referer="-"):
    return (f'{ip} - - [28/Oct/2026:11:40:02 +0100] "{request}" {status} 31 "{referer}" '
            f'"Mozilla/5.0 (X11; Linux x86_64) \\x22quoted\\x22 401"')  # nginx escapes " as \x22


def test_fail2ban_filter():
    """Refusals (401, 403, 429) count, with the client's address; nothing else does, also not a 401
    in the size, the referer or the user agent of another answer."""
    regexes = _filter()

    def host(line):
        for regex in regexes:
            match = regex.search(line)
            if match:
                return match.group("host")
        return None

    for status in (401, 403, 429):
        assert host(_line(status)) == "203.0.113.7"
    assert host(_line(429, "POST /api/estimate HTTP/1.1", ip="2001:db8::5")) == "2001:db8::5"
    assert host(_line(401, "GET /s/<token>/data.js HTTP/2.0")) == "203.0.113.7"
    for status in (200, 204, 301, 304, 400, 404, 500, 502, 503):
        assert host(_line(status)) is None, status
    assert host(_line(200, 'GET /api/x" 401 0 "x HTTP/2.0'.replace('"', '\\x22'))) is None  # nginx escapes "
    assert host(_line(200, referer="https://findaline.example.org/ 401 ")) is None
    assert host('203.0.113.7 - - [28/Oct/2026:11:40:02 +0100] "\\x16\\x03\\x01" 400 157 "-" "-"') is None


def _maps():
    """The map blocks of the http snippet: {variable: [(regex, value)]}, PCRE groups as Python's."""
    with open(os.path.join(SWAG, "00-findaline-http.conf"), encoding="utf-8") as f:
        text = f.read()
    maps = {}
    for source, target, body in re.findall(r"map \$(\w+) \$(\w+) \{(.*?)\n\}", text, re.S):
        entries = re.findall(r'^\s*"~(.*)" "(.*)";$', body, re.M)
        maps[target] = (source, [(re.compile(p.replace("(?<", "(?P<")), v) for p, v in entries])
    return maps


def _masked(maps, variable, value):
    for regex, template in maps[variable][1]:
        match = regex.search(value)
        if match:
            return re.sub(r"\$\{(\w+)\}", lambda m: match.group(m.group(1)) or "", template)
    return value


def test_access_log_masking():
    """Share tokens and the query of a sign-in callback never reach the log; the viewer's files
    and the other paths stay as they are."""
    maps = _maps()
    assert {name: source for name, (source, _) in maps.items()} == {
        "findaline_request": "request", "findaline_referer": "http_referer"}
    token = "-Ab_9" + "x" * 38
    request = lambda path: _masked(maps, "findaline_request", f"GET {path} HTTP/2.0")  # noqa: E731
    assert request(f"/s/{token}/data.js") == "GET /s/<token>/data.js HTTP/2.0"
    assert request(f"/s/{token}/") == "GET /s/<token>/ HTTP/2.0"
    assert request(f"/s/{token}") == "GET /s/<token> HTTP/2.0"
    assert request(f"/s/{token}/map.html?x=1") == "GET /s/<token>/map.html?x=1 HTTP/2.0"
    assert request("/auth/github?code=abc&state=def") == "GET /auth/github?<query> HTTP/2.0"
    for path in ("/s/_viewer/viewer.js", "/s/runs.js", "/s/index.html", "/s/runs.js?v=2", "/api/jobs",
                 "/runs/x/data.js", "/login/github?next=/", "/"):
        assert request(path) == f"GET {path} HTTP/2.0"
    referer = lambda url: _masked(maps, "findaline_referer", url)  # noqa: E731
    assert referer(f"https://findaline.example.org/s/{token}/map.html") == \
        "https://findaline.example.org/s/<token>/map.html"
    for url in ("https://findaline.example.org/s/_viewer/map.html", "https://findaline.example.org/app/", "-"):
        assert referer(url) == url
