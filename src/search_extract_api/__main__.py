"""``python -m search_extract_api`` / ``search-extract-api`` command."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the search-extract-api server.")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="Auto-reload (development).")
    parser.add_argument(
        "--generate-key",
        action="store_true",
        help="Print a new SESSION_ENCRYPTION_KEY and exit.",
    )
    args = parser.parse_args()

    if args.generate_key:
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return

    import uvicorn

    uvicorn.run(
        "search_extract_api.main:app_factory",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
