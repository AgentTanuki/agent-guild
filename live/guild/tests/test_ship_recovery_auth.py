"""Exercise the recovery workflow's push against a local Git HTTP server.

Only invented tokens and temporary repositories are used. An inherited checkout
header must not override the explicitly selected recovery credential: a push
authenticated with GITHUB_TOKEN cannot trigger the required push-event CI.
"""
import base64
import http.server
import os
from pathlib import Path
import re
import subprocess
import threading
import urllib.parse


def test_recovery_push_uses_its_token_and_preserves_checkout_auth(tmp_path):
    checkout = "Basic " + base64.b64encode(b"x-access-token:dummy-checkout").decode()
    recovery = "Basic " + base64.b64encode(b"x-access-token:dummy-recovery").decode()
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
           "GIT_ASKPASS": "/usr/bin/false", "NO_PROXY": "127.0.0.1"}
    work = tmp_path / "work"
    work.mkdir()

    def git(*args, cwd=work):
        return subprocess.run(["git", "-c", "credential.helper=", *args],
                              cwd=cwd, env=env, check=True,
                              capture_output=True, timeout=10)

    git("init", "-q")
    git("-c", "user.name=Local test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-qm", "local fixture")
    remote = tmp_path / "repository.git"
    git("init", "--bare", "-q", str(remote))
    git("config", "http.receivepack", "true", cwd=remote)
    observed = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_git(self):
            headers = self.headers.get_all("Authorization", [])
            observed.append([h.lower() for h in headers])
            if not headers:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="local-fixture"')
                self.end_headers()
                return
            url = urllib.parse.urlsplit(self.path)
            payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            cgi_env = {**env, "GIT_PROJECT_ROOT": str(tmp_path),
                       "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": url.path,
                       "QUERY_STRING": url.query, "REQUEST_METHOD": self.command,
                       "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                       "CONTENT_LENGTH": str(len(payload)), "REMOTE_USER": "fixture"}
            response = subprocess.run(["git", "http-backend"], env=cgi_env,
                                      input=payload, capture_output=True,
                                      check=True, timeout=10).stdout
            head, body = response.split(b"\r\n\r\n", 1)
            fields = [line.decode().split(":", 1) for line in head.split(b"\r\n")]
            status = next((int(value.strip().split()[0]) for name, value in fields
                           if name.lower() == "status"), 200)
            self.send_response(status)
            for name, value in fields:
                if name.lower() != "status":
                    self.send_header(name, value.strip())
            self.end_headers()
            self.wfile.write(body)

        do_GET = handle_git
        do_POST = handle_git

    workflow = (Path(__file__).resolve().parents[3]
                / ".github/workflows/ship.yml").read_text()
    # Execute the actual narrow push command, including its Git options. No
    # other workflow commands, credentials, or GitHub APIs enter this fixture.
    command = re.search(
        r'(?m)^            (git[^\n]+\\\n'
        r'              "https://x-access-token:\$\{RECOVERY_PUSH_TOKEN\}@github.com/\$REPO.git" \\\n'
        r'              "ship/revert-\$short")', workflow)
    assert command, "Recovery push command must remain covered by this fixture"

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host = f"127.0.0.1:{server.server_port}"
        origin = f"http://{host}"
        key = f"http.{origin}/.extraheader"
        git("config", "--local", key, "AUTHORIZATION: " + checkout)
        try:
            # Demonstrate the original failure: URL userinfo loses to the
            # inherited header, even though the Git push itself succeeds.
            git("push", f"http://x-access-token:dummy-recovery@{host}/repository.git",
                "HEAD:refs/heads/baseline")
            assert observed and all(row == [checkout.lower()] for row in observed)
            observed.clear()
            git("branch", "ship/revert-fixed")
            local_command = command.group(1).replace("https://", "http://").replace("github.com", host)
            subprocess.run(["bash", "-euc", local_command], cwd=work,
                           env={**env, "RECOVERY_PUSH_TOKEN": "dummy-recovery",
                                "REPO": "repository", "short": "fixed"},
                           check=True, capture_output=True, timeout=10)
            authenticated = [row for row in observed if row]
            assert authenticated and all(row == [recovery.lower()] for row in authenticated)
            assert git("rev-parse", "refs/heads/ship/revert-fixed", cwd=remote).stdout == git("rev-parse", "HEAD").stdout
            assert git("config", "--local", "--get", key).stdout.decode().strip() == "AUTHORIZATION: " + checkout
        finally:
            server.shutdown()
            thread.join(timeout=2)
