import uvicorn

from .app import create_app
from .config import Settings


def main():
    s = Settings.load()
    if s.bind not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding to {s.bind}. Only loopback is recommended; cloudflared reaches it locally.")
    uvicorn.run(create_app(s), host=s.bind, port=s.port, proxy_headers=False,
                server_header=False, log_level="info")


if __name__ == "__main__":
    main()
