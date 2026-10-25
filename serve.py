"""Start a loopback-only server and open the browser once it is listening."""
import argparse
import threading
import time
import urllib.request
import webbrowser

import uvicorn


def open_when_ready(url):
    for _ in range(60):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=0.5) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            time.sleep(0.25)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Локальный веб-интерфейс Whisper")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть от 1 до 65535")
    url = f"http://127.0.0.1:{args.port}"
    print(f"Whisper: {url}\nДля остановки нажмите Ctrl+C.", flush=True)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    uvicorn.run("web:app", host="127.0.0.1", port=args.port, workers=1)
