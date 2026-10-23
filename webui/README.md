# TradingAgents web dashboard

Live Binance market dashboard for TradingAgents: watchlist, candlestick chart with
indicators, 24h trends, and an assistant (chat + full multi-agent analysis).

Vite + React + TypeScript + Tailwind + shadcn/ui + lightweight-charts v5.

- Live prices and candles come straight from Binance's public market-data WebSocket
  (`wss://data-stream.binance.vision`). They keep working when the backend is down.
- History, indicators, trends, chat and analysis come from the TradingAgents API
  through `/api`. If the API is unreachable, those panels say so.

## Development

```bash
cd webui
npm install
npm run dev        # http://localhost:5173, proxies /api -> http://127.0.0.1:8000
```

Start the backend in another terminal from the repository root:

```bash
tradingagents-web  # or: python -m tradingagents.web
```

## Build

```bash
npm run build      # type-checks, then writes webui/dist
npm run lint
```

When `webui/dist` exists, the backend serves the dashboard at http://127.0.0.1:8000/.
