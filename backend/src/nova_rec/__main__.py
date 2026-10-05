"""`python -m nova_rec migrate | recorder`.

The API runs with `uvicorn --factory nova_rec.api:app_factory`.
"""

import argparse
import logging
import signal
import sys
import threading

from nova_rec.crypto import TokenCipher
from nova_rec.db import create_db_engine, create_session_factory, upgrade
from nova_rec.loop import RecorderLoop
from nova_rec.settings import get_settings


def _stop_on_signals() -> threading.Event:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    return stop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nova_rec")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply database migrations, then exit")
    commands.add_parser("recorder", help="always-on recorder that follows NOVA's push")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(name)s: %(message)s")
    url = settings.database_url.get_secret_value()
    if args.command == "migrate":
        upgrade(url)
        return 0

    key = settings.broker_token_key
    cipher = TokenCipher(key.get_secret_value()) if key is not None else None
    if cipher is None:
        logging.getLogger("nova.rec").warning(
            "NOVA_BROKER_TOKEN_KEY is not set: the recorder only waits"
        )
    engine = create_db_engine(url)
    try:
        RecorderLoop(
            factory=create_session_factory(engine), cipher=cipher, stop=_stop_on_signals()
        ).run()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
