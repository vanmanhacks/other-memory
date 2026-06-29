# Other Memory

Self-hosted search and crawl for AI agents. Zero API costs. Works with Hermes Agent, Claude Code, Codex, and any MCP-compatible client.

**What it does:**
- `web_search` — Meta search across Google, Bing, DuckDuckGo, Brave via SearXNG with Redis caching (1hr TTL)
- `web_crawl` — JavaScript-rendered page extraction via Crawl4AI with SSRF protection

## Architecture

```
AI Agent (Hermes) → MCP stdio → MetaMCP bridge → SearXNG (search) + Crawl4AI (crawl)
                                                    ↑                    ↑
                                                Redis cache            Headless browser
```

## Quick Start

### Prerequisites
- Docker and Docker Compose
- Python 3.10+
- Hermes Agent (or any MCP client)

### 1. Clone and configure
```bash
git clone https://github.com/vanmanhacks/other-memory.git
cd other-memory
cp .env.example .env
# Edit .env with your values
```

### 2. Start the stack
```bash
docker compose up -d
# Verify: curl http://localhost:8888/search?format=json&q=test
```

### 3. Register with Hermes
```bash
hermes mcp add other-memory --command /path/to/other-memory/metamcp/run.sh
# Restart your session to load the new tools
```

## Configuration

| Env var | Required | Description |
|---------|----------|-------------|
| `SEARXNG_SECRET` | Yes | Random string for SearXNG session encryption |
| `CRAWL4AI_TOKEN` | Yes | Token for Crawl4AI API access |
| `REDIS_URL` | No | Redis connection string (default: `redis://localhost:6380/0`) |

## Ports

| Service | Port | Purpose |
|---------|------|---------|
| SearXNG | 8888 | Meta search engine |
| Crawl4AI | 11235 | Headless browser crawler |
| Redis | 6380 | Query result cache |

## Security

- No API keys required — fully self-hosted
- SSRF protection blocks internal/private IPs
- Rate limiting at SearXNG level (30 req/min)
- Redis cache amortizes repeated queries

## License

MIT — see [LICENSE](LICENSE)
