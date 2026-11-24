from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import unittest
from unittest.mock import patch

import serve


@contextmanager
def health_server(identity):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(identity).encode())

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class LauncherTests(unittest.TestCase):
    def test_repeated_start_reuses_same_project_and_exits_successfully(self):
        body = {'status':'ok', 'app':'whisper-local', 'instance_id':serve.INSTANCE_ID}
        with health_server(body) as port, patch('serve.uvicorn.Server') as launch, patch('serve.webbrowser.open') as browser:
            self.assertEqual(serve.main(['--port', str(port)]), 0)
            launch.assert_not_called()
            browser.assert_called_once_with(f'http://127.0.0.1:{port}')

    def test_busy_port_from_another_project_uses_a_free_port(self):
        with health_server({'status':'ok', 'app':'whisper-local', 'instance_id':'another-copy'}) as port:
            listener, chosen = serve.select_listener(port)
            try:
                self.assertIsNotNone(listener)
                self.assertGreater(chosen, port)
                self.assertEqual(listener.getsockname(), ('127.0.0.1', chosen))
            finally:
                listener.close()

    def test_generic_health_endpoint_is_not_reused(self):
        with health_server({'status':'ok'}) as port:
            self.assertFalse(serve.is_our_server(f'http://127.0.0.1:{port}'))

    def test_selected_socket_stays_reserved_until_server_start(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        with patch('serve.is_our_server', return_value=False):
            first, chosen = serve.select_listener(port)
            second, next_port = serve.select_listener(port)
        try:
            self.assertEqual(chosen, port)
            self.assertGreater(next_port, port)
        finally:
            first.close()
            second.close()

    def test_browser_waits_for_this_project_to_be_ready(self):
        with patch('serve.is_our_server', side_effect=[False, True]), patch('serve.time.sleep'), \
                patch('serve.webbrowser.open') as browser:
            serve.open_when_ready('http://127.0.0.1:8001')
            browser.assert_called_once_with('http://127.0.0.1:8001')


if __name__ == '__main__':
    unittest.main()
