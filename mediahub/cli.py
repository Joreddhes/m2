from __future__ import annotations

import argparse
import os

from waitress import serve

from . import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="MediaHub — домашняя медиатека")
    parser.add_argument("--host", default=os.environ.get("MEDIAHUB_HOST", "0.0.0.0"))
    parser.add_argument("--port", default=int(os.environ.get("MEDIAHUB_PORT", "8080")), type=int)
    args = parser.parse_args()
    serve(create_app(), host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()

