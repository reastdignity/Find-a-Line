"""The deployment files: what each service of compose.yaml may write and publish, and the files of
the host (deploy/nginx, deploy/fail2ban): the fail2ban filter and the masking of the access log,
with Python's re (fail2ban's own engine; nginx's PCRE patterns differ only in the spelling of named
groups). nginx itself runs in tests/nginx_check.sh (CI, the docker job)."""

import configparser
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")
HOST = r"(?P<host>[0-9A-Fa-f:.]+|[\w\-.^_]*\w)"  # what fail2ban puts for <HOST> (simplified)


def _filter():
    """The failregex and ignoreregex lines of the filter, compiled."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(os.path.join(DEPLOY, "fail2ban", "findaline.conf"), encoding="utf-8")
    return [[re.compile(line.strip().replace("<HOST>", HOST))
             for line in parser["Definition"][key].splitlines() if line.strip()]
            for key in ("failregex", "ignoreregex")]


def _line(status, request='GET /api/jobs HTTP/2.0', ip="203.0.113.7", referer="-"):
    return (f'{ip} - - [28/Oct/2026:11:40:02 +0100] "{request}" {status} 31 "{referer}" '
            f'"Mozilla/5.0 (X11; Linux x86_64) \\x22quoted\\x22 401"')  # nginx escapes " as \x22


def test_fail2ban_filter():
    """Refusals (401, 403, 429) count, with the client's address; nothing else does, also not a 401
    in the size, the referer or the user agent of another answer; nor a 429 of an orthophoto tile."""
    regexes, ignored = _filter()

    def host(line):
        if any(regex.search(line) for regex in ignored):
            return None
        for regex in regexes:
            match = regex.search(line)
            if match:
                return match.group("host")
        return None

    for status in (401, 403, 429):
        assert host(_line(status)) == "203.0.113.7"
    assert host(_line(429, "POST /api/estimate HTTP/1.1", ip="2001:db8::5")) == "2001:db8::5"
    assert host(_line(401, "GET /s/<token>/data.js HTTP/2.0")) == "203.0.113.7"
    tile = "GET /cache/poland/ortho/live/wm256/13/4551/2774.jpg HTTP/2.0"
    assert host(_line(429, tile)) is None
    assert host(_line(403, tile)) == "203.0.113.7"
    assert host(_line(429, "GET /api/x?next=/cache/a HTTP/2.0")) == "203.0.113.7"
    assert host(_line(429, "GET /api/x HTTP/2.0", referer="https://findaline.example.org/cache/a"))         == "203.0.113.7"
    for status in (200, 204, 301, 304, 400, 404, 500, 502, 503):
        assert host(_line(status)) is None, status
    assert host(_line(200, 'GET /api/x" 401 0 "x HTTP/2.0'.replace('"', '\\x22'))) is None  # nginx escapes "
    assert host(_line(200, referer="https://findaline.example.org/ 401 ")) is None
    assert host('203.0.113.7 - - [28/Oct/2026:11:40:02 +0100] "\\x16\\x03\\x01" 400 157 "-" "-"') is None


def _maps():
    """The map blocks of the http part: {variable: [(regex, value)]}, PCRE groups as Python's."""
    with open(os.path.join(DEPLOY, "nginx", "findaline-http.conf"), encoding="utf-8") as f:
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


def _services():
    """The services of compose.yaml (its own layout, without a YAML library): {name: {"volumes":
    [(source, target, options)], "environment": {name: value}, key: value}}; a tmpfs mount is
    ("tmpfs", target, ""); an inline list (`ports: [...]`) stays the text of the list."""
    with open(os.path.join(ROOT, "compose.yaml"), encoding="utf-8") as f:
        lines = [line.split(" #")[0].rstrip() for line in f.read().splitlines()]
    services, current, section = {}, None, None
    for line in lines[lines.index("services:") + 1:]:
        if line and not line.startswith(" "):
            break  # the next top-level key
        if re.fullmatch(r"  [\w-]+:", line):
            current = services[line.strip()[:-1]] = {"volumes": [], "environment": {}, "ports": "[]"}
        elif current is None or not line.strip() or line.strip().startswith("#"):
            continue
        elif match := re.fullmatch(r"    ([\w-]+):(?: (.*))?", line):
            section = match.group(1)
            if match.group(2):
                current[section] = match.group(2)
        elif section == "volumes" and line.startswith("      - "):
            item = line[8:]
            short = re.fullmatch(r"(.+?):(/[^:]*)(?::(\w+))?", item)  # source:target[:options]
            current["volumes"].append(("tmpfs", None, "") if item == "type: tmpfs"
                                      else (short.group(1), short.group(2), short.group(3) or ""))
        elif section == "volumes" and (match := re.fullmatch(r"        target: (\S+)", line)):
            current["volumes"][-1] = ("tmpfs", match.group(1), "")
        elif section == "environment" and (match := re.fullmatch(r"      (\w+): (.*)", line)):
            current["environment"][match.group(1)] = match.group(2)
    return services


def test_compose_volumes():
    """What each service can write (docs/deployment.md, "What each service can write"): the
    accounts volume is the web service's only; numba's compiled code, the home and matplotlib's
    settings are in each service's own tmpfs, never in the shared data volume; no swap beyond the
    memory limit."""
    services = _services()
    assert set(services) == {"web", "worker"}
    web, worker = services["web"], services["worker"]
    assert web["volumes"] == [("data", "/data", ""), ("accounts", "/accounts", ""), ("tmpfs", "/tmp", "")]
    assert worker["volumes"] == [("data", "/data", ""), ("${FINDALINE_MIRROR_DIR:-no-mirror}", "/mirror", "ro"),
                                 ("tmpfs", "/tmp", "")]
    assert web["environment"]["FINDALINE_ACCOUNTS_DIR"] == "/accounts"
    assert "FINDALINE_ACCOUNTS_DIR" not in worker["environment"]
    for service in (web, worker):
        for name in ("HOME", "NUMBA_CACHE_DIR", "MPLCONFIGDIR"):
            assert service["environment"][name].startswith("/tmp/"), name
        assert service["memswap_limit"] == service["mem_limit"]


def test_compose_ports():
    """Only the web service publishes a port, on the host's loopback (nginx on the host proxies to
    it, deploy/nginx/findaline-http.conf); its network has the fixed gateway that
    findaline.env.example trusts as the proxy."""
    services = _services()
    assert services["web"]["ports"] == '["127.0.0.1:${WEB_PORT:-8765}:8765"]'
    assert services["worker"]["ports"] == "[]"
    with open(os.path.join(ROOT, "compose.yaml"), encoding="utf-8") as f:
        assert "gateway: 172.30.0.1" in f.read()
    with open(os.path.join(ROOT, "findaline.env.example"), encoding="utf-8") as f:
        assert "FINDALINE_TRUSTED_PROXY=172.30.0.1\n" in f.read()
    with open(os.path.join(DEPLOY, "nginx", "findaline-http.conf"), encoding="utf-8") as f:
        assert "server 127.0.0.1:8765;" in f.read()
