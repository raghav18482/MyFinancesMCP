# MyFinanceMCP — Angel One Portfolio Tracker

A multi-user MCP server and web dashboard for tracking your Angel One portfolio, positions, orders, and P&L in real time.

## Features

- **MCP Server (SSE transport)** — connect from Cursor, Claude Desktop, or any MCP-compatible client
- **Web Dashboard** — browser-based portfolio viewer with login, holdings, positions, and orders pages
- **Multi-user** — each user authenticates with their own Angel One credentials
- **Zero credential storage** — credentials live only in server memory for the session duration (max 8 hours)

## Project Structure

```
├── main.py              # Combined entry point (MCP + Web on one port)
├── server.py            # MCP server with all tools (login, portfolio, trading)
├── web_app.py           # FastAPI web dashboard
├── angel_client.py      # Angel One SmartAPI client wrapper
├── session_manager.py   # Per-session client management
├── templates/           # Jinja2 HTML templates
├── static/              # CSS
├── requirements.txt     # Python dependencies
├── Dockerfile           # Container definition
└── .dockerignore
```

## Run Locally

```bash
pip install -r requirements.txt
python main.py
```

The server starts on `http://localhost:8000`:
- Web dashboard: `http://localhost:8000/`
- MCP SSE endpoint: `http://localhost:8000/mcp/sse`

## Connect from an MCP Client

### Cursor

Add to your `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "angelone-portfolio": {
      "url": "http://localhost:8000/mcp/sse"
    }
  }
}
```

Then call the `login` tool with your Angel One credentials to start a session.

### Claude Desktop

Add to your Claude Desktop config:

```json
{
  "mcpServers": {
    "angelone-portfolio": {
      "url": "http://localhost:8000/mcp/sse"
    }
  }
}
```

## Deploy to Railway (Recommended)

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) and create a new project
3. Connect your GitHub repository
4. Railway auto-detects the Dockerfile and deploys
5. Set the environment variable `PORT=8000` (Railway usually injects this automatically)
6. Get your public URL (e.g. `https://myfinancemcp.up.railway.app`)

### After Deployment

**Web users:** Visit `https://your-app.up.railway.app` and log in with Angel One credentials.

**MCP users:** Update the `url` in your MCP client config:

```json
{
  "mcpServers": {
    "angelone-portfolio": {
      "url": "https://your-app.up.railway.app/mcp/sse"
    }
  }
}
```

## Deploy to Render (Free Tier)

1. Push to GitHub
2. Go to [render.com](https://render.com), create a new **Web Service**
3. Connect your repo, select **Docker** as the runtime
4. Set environment variable `PORT=8000`
5. Deploy

## Available MCP Tools

| Tool | Description |
|---|---|
| `login` | Authenticate with Angel One (must call first) |
| `logout` | Discard session credentials |
| `get_profile` | Account profile |
| `get_holdings` | Stock holdings with P&L |
| `get_all_holdings` | Complete holdings overview |
| `get_positions` | Open positions |
| `get_order_book` | Today's orders |
| `get_trade_book` | Executed trades |
| `get_funds` | Account funds / margin |
| `get_ltp` | Last traded price |
| `search_scrip` | Search stocks by name |
| `portfolio_summary` | High-level portfolio summary |
| `get_candle_data` | Historical OHLC data |
| `place_order` | Place a new order |
| `modify_order` | Modify an existing order |
| `cancel_order` | Cancel an order |

## Security

- Credentials are **never written to disk** — they exist only in server memory
- Sessions expire automatically after **8 hours**
- All traffic should go over **HTTPS** (provided by Railway/Render by default)
- The web dashboard uses encrypted session cookies

## License

MIT
