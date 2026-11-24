"""Start a loopback-only server and open the browser once it is listening."""
import argparse
import errno
import json
import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn
from server_identity import INSTANCE_ID


def is_our_server(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + '/api/health', timeout=.5) as response:
            body = json.loads(response.read(16384))
        return (isinstance(body, dict) and body.get('status') == 'ok'
                and body.get('app') == 'whisper-local' and body.get('instance_id') == INSTANCE_ID)
    except (OSError, ValueError):
        return False


def select_listener(port):
    for candidate in range(port, min(port + 21, 65536)):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(('127.0.0.1', candidate))
            # Hold the socket until Uvicorn takes it, preventing another launch
            # from claiming the selected port between checking and startup.
            listener.listen(128)
            return listener, candidate
        except OSError as error:
            listener.close()
            if error.errno not in (errno.EADDRINUSE, errno.EACCES) and getattr(error, 'winerror', None) not in (10048, 10013):
                raise
            if is_our_server(f'http://127.0.0.1:{candidate}'):
                return None, candidate
    raise OSError('Не найден свободный порт. Закройте лишние копии или задайте другой --port.')


def open_when_ready(url):
    for _ in range(60):
        try:
            if is_our_server(url):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.25)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локальный веб-интерфейс Whisper")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть от 1 до 65535")
    try:
        listener, port = select_listener(args.port)
    except OSError as error:
        print(f'Не удалось запустить сервер: {error}', flush=True)
        return 1
    url = f"http://127.0.0.1:{port}"
    if listener is None:
        print(f'Эта копия Whisper уже работает: {url}', flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    if port != args.port:
        print(f'Порт {args.port} занят. Использую {port}.', flush=True)
    print(f"Whisper: {url}\nДля остановки нажмите Ctrl+C.", flush=True)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    try:
        server = uvicorn.Server(uvicorn.Config('web:app', host='127.0.0.1', port=port, workers=1))
        server.run(sockets=[listener])
        return 0 if server.started else 1
    finally:
        listener.close()


if __name__ == '__main__':
    raise SystemExit(main())
