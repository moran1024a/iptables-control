"""Synchronous wire harness for existing protocol tests; production uses aiohttp."""
import http.server
import threading
import api
from resources import Resources

class LimitedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    max_connections = 64

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(self.max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

class Handler(Resources, http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    panel_path = '/'
    sync_path = '/'
    timeout = 15
    def __init__(self, *args):
        http.server.BaseHTTPRequestHandler.__init__(self, *args)
    def log_message(self, *args): pass
    def run_request(self):
        length = int(self.headers.get('Content-Length', '0'))
        request = api.Request(self.command, self.path, self.headers.items(), self.rfile.read(length), self.client_address[0], panel_path=self.panel_path, sync_path=self.sync_path)
        request.route(self.command)
        code, data, headers = request.response
        self.send_response(code)
        for key, value in headers: self.send_header(key, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    do_GET = do_POST = do_PATCH = do_DELETE = run_request
