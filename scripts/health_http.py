"""DEL-05 fixed loopback readiness probe: never follow redirects or proxy env."""

import http.client
import json
import os
import socket
import sys


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/marketing-agents/api.sock")


connection = (
    UnixConnection("localhost", timeout=4)
    if os.environ.get("MARKETING_AGENTS_API_SOCKET")
    else http.client.HTTPConnection("127.0.0.1", 8000, timeout=4)
)
try:
    connection.request("GET", "/health/ready", headers={"Host": "127.0.0.1:8000"})
    response = connection.getresponse()
    payload = response.read(65_537)
    ready = response.status == 200 and len(payload) <= 65_536
    if ready:
        decoded = json.loads(payload)
        ready = isinstance(decoded, dict) and decoded.get("status") == "ready"
except (OSError, ValueError, http.client.HTTPException):
    ready = False
finally:
    connection.close()
sys.exit(0 if ready else 1)
