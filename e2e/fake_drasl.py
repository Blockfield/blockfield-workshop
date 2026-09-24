"""Local stand-in for the Drasl endpoints the blockfield-proxy plugin calls. Stand only; any password logs in.

  python3 fake_drasl.py <port> [name:draslId ...]   # default profiles: Player115, RigPeer, RigPeer3
"""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

PROFILES = {'Player115': '343a227845793a598a3aaa2690a75202', 'RigPeer': '6727890496a93613a33463780ae01220',
            'RigPeer3': '0c6f3b0e5b7a4b8f9d2e1a3c4b5d6e7f'}
for arg in sys.argv[2:]:
    name, _, draslId = arg.partition(':')
    if draslId:
        PROFILES[name] = draslId


class H(BaseHTTPRequestHandler):
    def reply(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b''
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith('/account/users/profiles/minecraft/'):
            name = self.path.rsplit('/', 1)[1]
            return self.reply(200, {'id': PROFILES[name], 'name': name}) if name in PROFILES else self.reply(404, {})
        self.reply(204)  # hasJoined: never joined (offline rig clients use /login + remembered session)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        if self.path == '/auth/authenticate':
            name = body.get('username')
            if name not in PROFILES:
                return self.reply(403, {'errorMessage': 'invalid'})
            return self.reply(200, {'accessToken': uuid4().hex, 'clientToken': uuid4().hex,
                                    'selectedProfile': {'id': PROFILES[name], 'name': name}})
        if self.path == '/auth/invalidate':
            return self.reply(204)
        if self.path == '/drasl/api/v3/users':
            return self.reply(200, {}) if body.get('playerName') in PROFILES else self.reply(400, {'message': 'unknown'})
        self.reply(404, {})

    def log_message(self, fmt, *args):
        sys.stderr.write('drasl ' + fmt % args + '\n')


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
