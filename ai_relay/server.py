"""Сетевая оболочка посредника на стандартной библиотеке. Вся логика живёт в ai_relay.relay."""

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_relay.relay import Relay

REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 411: "Length Required",
    413: "Content Too Large", 415: "Unsupported Media Type", 429: "Too Many Requests",
    502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout",
}


def make_handler(relay: Relay):
    class Handler(BaseHTTPRequestHandler):
        server_version = "beresta-ai-relay"
        sys_version = ""
        # Читать запрос дольше этого времени нельзя. Так медленный клиент не держит поток.
        timeout = 60

        def serve(self):
            headers = {name.lower(): value for name, value in self.headers.items()}
            reply = relay.handle(self.command, self.path, headers, self.rfile)
            try:
                self.send_response(reply.status, REASONS.get(reply.status, "Error"))
                for name, value in reply.headers:
                    self.send_header(name, value)
                if isinstance(reply.body, bytes):
                    self.send_header("Content-Length", str(len(reply.body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if isinstance(reply.body, bytes):
                    self.wfile.write(reply.body)
                else:
                    for chunk in reply.body:
                        self.wfile.write(chunk)
                        self.wfile.flush()
            finally:
                if reply.close:
                    reply.close()
            self.close_connection = True

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = serve

        def log_message(self, format, *args):
            """Строку запроса пишет сам Relay, здесь ничего не нужно."""

    return Handler


def make_server(relay: Relay, host, port):
    server = ThreadingHTTPServer((host, port), make_handler(relay))
    server.daemon_threads = True
    return server


def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
