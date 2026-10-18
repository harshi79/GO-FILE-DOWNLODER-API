"""Entry point: ``python -m gofile_bot``."""

from __future__ import annotations

import argparse
import logging
import sys


def _configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gofile_bot",
        description="GoFile.io Telegram File Downloader Bot",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="verbose logging (DEBUG level)",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="path to the .env file (default: ./.env)",
    )
    args = parser.parse_args(argv)

    _configure_logging(args.debug)

    from gofile_bot.config import load_settings

    try:
        settings = load_settings(args.env_file)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from gofile_bot.app import run

    run(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
