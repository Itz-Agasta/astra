# Astra MCP

MCP server that lets Claude Desktop and Cursor control a telescope through natural language. You say "point to Jupiter" — Astra figures out the rest.

## Architecture

```
Claude Desktop / Cursor
        │
        │  MCP (stdio)
        ▼
  astra-mcp
  (this server)
        │
        │  HTTP (httpx)
        ▼
  Astra Backend API
  (telescope logic, plate solving,
   calibration, camera, hardware)
        │
        ▼
  Telescope Hardware
```

The MCP server is intentionally thin:
1. Receive tool call from Claude
2. Validate arguments
3. Forward to backend
4. Return result

No astronomy logic lives here.

---

## Installation

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

### Setup

```bash
# Clone / copy the project
cd mcp

# Create virtualenv and install dependencies
uv sync

# Copy environment config
cp .env.example .env
```

Edit `.env` to point at your backend:

```
BACKEND_BASE_URL=http://localhost:8000
```

### Run the server (standalone test)

```bash
uv run astra-mcp
```

The server starts in stdio mode, waiting for an MCP client to connect.
You should see:

```
Astra MCP server starting...
7 tools registered and ready
Waiting for MCP client (Claude Desktop / Cursor)...
```

---

## Connecting to Claude Desktop

1. Open Claude Desktop → **Settings → Developer → Edit Config**

2. Add the following to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "astra-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/mcp", "astra-mcp"],
      "env": {
        "BACKEND_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
```