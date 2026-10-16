# VCP Fleet Simulator — Control & Management Plane

A React + FastAPI control plane for the VCP Fleet Simulator. Provides a REST/WebSocket API and browser UI for registering, monitoring, and commanding virtual charge points (VCPs).

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    React SPA (Vite)                          │
│  Dashboard · Fleet · VCPs · Profiles · Scenarios · Events   │
└────────────────────────┬─────────────────────────────────────┘
                         │ REST + WebSocket
┌────────────────────────▼─────────────────────────────────────┐
│              FastAPI Control Plane  (:8000)                   │
│  /api/v1/vcps  /fleet  /profiles  /scenarios  /events/ws     │
│                                                              │
│  VcpRegistryStore (PostgreSQL)  ←→  Alembic migrations      │
│  CommandPublisher  →  Redis Streams  →  Data Plane Workers   │
│  EventSubscriber   ←  Redis Streams  ←  Data Plane Workers   │
└──────────────────────────────────────────────────────────────┘
```

### Redis Stream Keys (matching vcp_data_plane)

| Direction        | Stream Key Pattern                      |
|------------------|-----------------------------------------|
| Control → Worker | `vcp:commands:worker:{worker_id}`       |
| Control → All    | `vcp:commands:broadcast`                |
| Worker → Control | `vcp:events:worker:{worker_id}`         |

## Quick Start

### Prerequisites

- Python 3.11+
- Node 20+
- Docker + Docker Compose (optional)

### With Docker Compose

```bash
cp .env.example .env        # Edit VCP_CP_API_TOKEN and VCP_CP_WORKER_URLS_RAW
docker-compose up -d
# Backend: http://localhost:8000
# Frontend: http://localhost:5173
```

### Local Development

**Backend:**

```bash
cd vcp_control_plane
pip install -e ".[dev]"

# Set environment variables (or copy .env.example → .env and edit)
export VCP_CP_DATABASE_URL="postgresql+asyncpg://user:pass@localhost/vcp_registry"
export VCP_CP_REDIS_HOST=localhost
export VCP_CP_API_TOKEN=my-dev-token

# Run migrations
vcp-control db migrate

# Start the server (hot-reload)
vcp-control run --reload
```

**Frontend:**

```bash
cd vcp_control_plane/frontend
npm install

# Create .env.local
cat > .env.local <<EOF
VITE_API_BASE_URL=http://localhost:8000/api/v1
VITE_WS_BASE_URL=ws://localhost:8000/api/v1
VITE_API_TOKEN=my-dev-token
EOF

npm run dev
```

## Environment Variables

All backend variables use the `VCP_CP_` prefix. See `.env.example` for the full list.

| Variable                     | Default                          | Description                         |
|------------------------------|----------------------------------|-------------------------------------|
| `VCP_CP_API_TOKEN`           | *(none)*                         | Bearer token for API auth           |
| `VCP_CP_REQUIRE_AUTH`        | `true`                           | Enforce bearer token                |
| `VCP_CP_DATABASE_URL`        | `sqlite+aiosqlite:///vcp.db`     | PostgreSQL (asyncpg) URL            |
| `VCP_CP_REDIS_HOST`          | `localhost`                      | Redis host                          |
| `VCP_CP_REDIS_PORT`          | `6379`                           | Redis port                          |
| `VCP_CP_WORKER_URLS_RAW`     | *(empty)*                        | Comma-separated data-plane URLs     |
| `VCP_CP_COMMAND_STREAM_PREFIX` | `vcp:commands:worker`          | Per-worker command stream prefix    |
| `VCP_CP_BROADCAST_STREAM`    | `vcp:commands:broadcast`         | Fleet broadcast stream key          |
| `VCP_CP_EVENT_STREAM_PREFIX` | `vcp:events:worker`              | Worker event stream prefix          |
| `VCP_CP_PROFILE_DIR`         | `./profiles`                     | Directory of YAML charger profiles  |

## API Reference

Interactive Swagger UI available at `http://localhost:8000/docs`.

### Key Endpoints

| Method | Path                              | Description                        |
|--------|-----------------------------------|------------------------------------|
| GET    | `/api/v1/vcps`                    | Paginated VCP list                 |
| POST   | `/api/v1/vcps`                    | Register a VCP                     |
| GET    | `/api/v1/vcps/{id}`               | VCP detail                         |
| PATCH  | `/api/v1/vcps/{id}`               | Update metadata / desired state    |
| POST   | `/api/v1/vcps/{id}/commands`      | Send command to VCP                |
| GET    | `/api/v1/fleet/summary`           | Aggregate fleet counts             |
| POST   | `/api/v1/fleet/commands`          | Bulk / broadcast command           |
| GET    | `/api/v1/profiles`                | List charger profiles              |
| POST   | `/api/v1/profiles/import-yaml`    | Import profile from YAML upload    |
| GET    | `/api/v1/scenarios`               | Built-in scenario catalogue        |
| POST   | `/api/v1/scenarios/run`           | Launch scenario execution          |
| WS     | `/api/v1/events/ws`               | Real-time event stream             |

### WebSocket Filters

Connect with query parameters to filter the event stream:

```
ws://localhost:8000/api/v1/events/ws?token=TOKEN&vcpId=vcp-001
ws://localhost:8000/api/v1/events/ws?token=TOKEN&groupId=group-alpha
ws://localhost:8000/api/v1/events/ws?token=TOKEN&events=state_change,meter_value
```

## Running Tests

```bash
pip install -e ".[dev]"
pytest -v
```

Tests use an in-memory SQLite database and `fakeredis` — no real infrastructure required.

## CLI Reference

```bash
vcp-control run [--host HOST] [--port PORT] [--reload] [--workers N]
vcp-control db migrate [REVISION]
vcp-control db downgrade [REVISION]
vcp-control db revision -m "message"
vcp-control db history
vcp-control db current
vcp-control profiles import-dir [--dir PATH]
vcp-control version
```

## Project Structure

```
vcp_control_plane/
├── src/vcp_control_plane/
│   ├── api/
│   │   ├── app.py          # FastAPI factory + lifespan
│   │   ├── auth.py         # Bearer token dependency
│   │   ├── deps.py         # DB / Redis / settings dependencies
│   │   ├── schemas.py      # Pydantic v2 request/response models
│   │   └── routers/
│   │       ├── vcps.py     # VCP CRUD + per-VCP commands
│   │       ├── fleet.py    # Fleet summary + bulk commands
│   │       ├── profiles.py # Charger profile management
│   │       ├── scenarios.py # Scenario execution
│   │       └── events.py   # WebSocket event streaming
│   ├── bus/
│   │   ├── publisher.py    # XADD to Redis Streams
│   │   └── subscriber.py   # XREADGROUP → DB + WebSocket
│   ├── config/settings.py  # Pydantic Settings (VCP_CP_ prefix)
│   ├── db/
│   │   ├── base.py         # SQLAlchemy async engine singleton
│   │   └── models.py       # ORM models (4 tables)
│   ├── registry/store.py   # CRUD layer over SQLAlchemy
│   └── cli.py              # Typer CLI
├── alembic/                # Async Alembic migrations
├── frontend/               # React + Vite SPA
│   └── src/
│       ├── api/            # API client modules
│       ├── components/     # Reusable UI components
│       ├── hooks/          # React hooks
│       └── pages/          # Route-level page components
├── tests/                  # pytest async test suite
├── docker/                 # Dockerfiles + entrypoint
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

## Connecting to vcp_data_plane

The control plane and data plane communicate via Redis Streams using the same stream key conventions. Configure `VCP_CP_WORKER_URLS_RAW` with the HTTP addresses of your data-plane worker processes so the fleet summary can query live status:

```env
VCP_CP_WORKER_URLS_RAW=http://worker-1:9000,http://worker-2:9000
VCP_CP_WORKER_TOKEN=your-worker-auth-token
```

The control plane will:
1. Publish `WorkerCommand` messages to `vcp:commands:worker:{worker_id}` (or broadcast stream)
2. Subscribe to `vcp:events:worker:*` streams and update the PostgreSQL registry in real-time
3. Forward events to connected WebSocket clients in the React UI
