"""Run the dashboard server: ``tradingagents-web`` or ``python -m tradingagents.web``.

``TRADINGAGENTS_WEB_HOST`` (default 127.0.0.1) and ``TRADINGAGENTS_WEB_PORT`` (default 8000)
set where it listens; the container listens on 0.0.0.0 and is published on 127.0.0.1 only.
"""

import os

import uvicorn
from dotenv import find_dotenv, load_dotenv


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    host = os.environ.get("TRADINGAGENTS_WEB_HOST") or "127.0.0.1"
    port = int(os.environ.get("TRADINGAGENTS_WEB_PORT") or 8000)
    # A SelectorEventLoop on Windows, which async psycopg needs (see tradingagents.db).
    uvicorn.run("tradingagents.web.app:app", host=host, port=port,
                loop="tradingagents.db:uvicorn_loop_factory")


if __name__ == "__main__":
    main()
