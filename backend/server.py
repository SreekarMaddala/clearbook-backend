"""Run python -m backend.server --seed-demo. Standalone local API on port 8000."""
import argparse
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .api import dispatch
from .database import connection, initialize
from .service import now, rebuild_allocations

ROOT = Path(__file__).resolve().parent.parent


def seed_demo():
    if os.environ.get('DYNAMODB_TABLE'):
        raise RuntimeError('Demo seeding is local-only. Do not seed sample data into the AWS table.')
    data = json.loads((ROOT / 'backend' / 'seed.json').read_text(encoding='utf-8'))
    with connection(write=True) as db:
        if db.all('accounts') or db.all('transactions'):
            return
        for account in data['accounts']:
            db.put('accounts', account['id'], {**account, 'created_at': now()})
        for vendor in data['vendors']:
            db.put('vendors', vendor['id'], {**vendor, 'created_at': now()})
        for t in data['transactions']:
            row = {**t, 'amount_paise': round(t['amount'] * 100), 'created_at': now(), 'updated_at': now(),
                   'version': 1, 'reversed_at': None, 'reversal_reason': None, 'history': '[]',
                   'receipt_name': None}
            row.pop('amount')
            db.put('transactions', t['id'], row)
        rebuild_allocations(db)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PATCH(self):
        self.handle_request()

    def handle_request(self):
        url = urlsplit(self.path)
        if url.path.startswith('/api/'):
            try:
                size = int(self.headers.get('Content-Length', 0))
                if size < 0 or size > 1024*1024:
                    self.respond(413, {'error':'Request body exceeds 1 MB.'})
                    return
                # The unauthenticated local development server only accepts same-origin writes.
                if self.command != 'GET' and self.headers.get('Origin') not in (None, f'http://{self.headers.get("Host")}'):
                    self.respond(403, {'error':'Cross-origin writes are not allowed.'})
                    return
                payload = self.rfile.read(size).decode('utf-8') if size else '{}'
                body = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                self.respond(400, {'error':'Invalid JSON request body.'})
                return
            status, data = dispatch(self.command, url.path, body, dict(parse_qsl(url.query)), dict(self.headers))
            self.respond(status, data)
            return
        if self.command == 'GET' and url.path == '/':
            self.respond(200, {'service': 'Clearbook API', 'routes': '/api', 'region': 'ap-south-2'})
        else:
            self.respond(404, {'error': 'API endpoint not found.'})

    def respond(self, status, payload):
        data = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = not hasattr(socket, 'SO_EXCLUSIVEADDRUSE')

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed-demo', action='store_true', help='Populate a new local database with sample transactions.')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    initialize()
    if args.seed_demo:
        seed_demo()
    server = LocalHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Clearbook API: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nServer stopped.')
