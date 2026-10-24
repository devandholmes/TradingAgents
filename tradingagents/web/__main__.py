"""Run the dashboard server: ``tradingagents-web`` or ``python -m tradingagents.web``."""

import uvicorn
from dotenv import find_dotenv, load_dotenv


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    # A SelectorEventLoop on Windows, which async psycopg needs (see tradingagents.db).
    uvicorn.run("tradingagents.web.app:app", host="127.0.0.1", port=8000,
                loop="tradingagents.db:uvicorn_loop_factory")


if __name__ == "__main__":
    main()
