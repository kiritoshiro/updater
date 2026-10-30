"""A tiny stand-in for the GitHub API on 127.0.0.1:8099, for the disposable app tests only.

Branch heads, compare results and archives come from the local bare repositories in REPOS. Gate
results come from /srv/fakegh/gates.json ({"<sha>": "passed" | "failed" | "pending"}); any other
commit has no gate run ("missing"). Every request is logged to /srv/fakegh/requests.log.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import parse_qs, urlsplit

REPOS = {"kiritoshiro/AdventOS": "/srv/origin/AdventOS.git",
         "kiritoshiro/vilnius-adventistai": "/srv/origin/vilnius.git",
         "kiritoshiro/edenoaidai": "/srv/origin/edenoaidai.git"}
STATE = Path("/srv/fakegh")


def git(repo, *args, text=True):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True, text=text).stdout


def branch_of(repo, sha):
    """The bare repository's only branch (each test repository has one)."""
    return git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split()[0]


def gate(sha):
    try:
        return json.loads((STATE / "gates.json").read_text()).get(sha, "missing")
    except (OSError, ValueError):
        return "missing"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, body, kind="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        with (STATE / "requests.log").open("a") as log:
            log.write(f"{url.path} auth={'yes' if self.headers.get('Authorization') else 'no'}\n")
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/(.+)", url.path) or re.fullmatch(r"/codeload/([^/]+/[^/]+)/(zip/.+)", url.path)
        if not m or m[1] not in REPOS:
            return self.reply(404, {"message": "Not Found"})
        repo, rest, query = REPOS[m[1]], m[2], parse_qs(url.query)
        try:
            if rest.startswith("commits/"):
                sha = git(repo, "rev-parse", "--verify", f"refs/heads/{rest[8:]}^{{commit}}").strip()
                message, date = git(repo, "log", "-1", "--format=%B%x00%cI", sha).split("\x00")
                return self.reply(200, {"sha": sha, "commit": {"message": message, "committer": {"date": date.strip()}}})
            if rest == "actions/runs":
                sha = query.get("head_sha", [""])[0]
                state = gate(sha)
                runs = [] if state == "missing" else [
                    {"id": int(sha[:8], 16), "head_sha": sha, "head_branch": query.get("branch", [None])[0] or branch_of(repo, sha),
                     "path": ".github/workflows/security-gate.yml", "event": "push",
                     "status": "in_progress" if state == "pending" else "completed"}]
                return self.reply(200, {"workflow_runs": runs})
            m2 = re.fullmatch(r"actions/runs/(\d+)/jobs", rest)
            if m2:
                sha = next((s for s in json.loads((STATE / "gates.json").read_text()) if int(s[:8], 16) == int(m2[1])), "")
                state = gate(sha)
                jobs = [] if state == "pending" else [{"name": "All security checks passed", "head_sha": sha, "status": "completed",
                                                       "conclusion": "success" if state == "passed" else "failure"}]
                return self.reply(200, {"jobs": jobs})
            m2 = re.fullmatch(r"compare/([0-9a-f]{40})\.\.\.([0-9a-f]{40})", rest)
            if m2:
                base, head = m2[1], m2[2]
                if base == head:
                    status = "identical"
                else:
                    ancestor = lambda a, b: subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", a, b]).returncode == 0
                    status = "ahead" if ancestor(base, head) else "behind" if ancestor(head, base) else "diverged"
                return self.reply(200, {"status": status})
            m2 = re.fullmatch(r"zipball/([0-9a-f]{40})", rest)
            if m2:  # like GitHub: a redirect to the archive host
                return self.reply(302, b"", "text/plain", [("Location", f"http://127.0.0.1:8099/codeload/{m[1]}/zip/{m2[1]}")])
            m2 = re.fullmatch(r"zip/([0-9a-f]{40})", rest)
            if m2:
                prefix = f"{m[1].replace('/', '-')}-{m2[1][:7]}/"
                data = subprocess.run(["git", "-C", repo, "archive", "--format=zip", f"--prefix={prefix}", m2[1]],
                                      capture_output=True, check=True).stdout
                return self.reply(200, data, "application/zip")
        except (subprocess.CalledProcessError, ValueError):
            return self.reply(404, {"message": "Not Found"})
        return self.reply(404, {"message": "Not Found"})


if __name__ == "__main__":
    STATE.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", 8099), Handler).serve_forever()
