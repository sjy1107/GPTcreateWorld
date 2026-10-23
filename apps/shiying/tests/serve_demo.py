"""Optional local page for trying the app without an external website."""
import functools
import http.server

from .integration_demo import FIXTURES, Handler, make_fixtures


def main():
    make_fixtures()
    handler = functools.partial(Handler, directory=str(FIXTURES))
    with http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler) as server:
        print("测试网页：http://127.0.0.1:8765/index.html", flush=True)
        print("保持此窗口运行；按 Ctrl+C 停止。", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
