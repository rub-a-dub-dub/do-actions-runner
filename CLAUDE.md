# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Important: Keep this file under 300 lines. Move detailed documentation to `docs/` folder.**

## Project Overview

Self-hosted GitHub Actions runner for DigitalOcean App Platform with auto-registration and horizontal scaling support.

## Architecture

- **Dockerfile** - Ubuntu image with latest GitHub Actions runner + CI/CD tooling (Node.js LTS, build tools)
- **entrypoint.sh** - Runner lifecycle: register on start, deregister on SIGTERM via cleanup trap

## Build & Run

```bash
docker build -t do-actions-runner .

# Repository-level
docker run -e TOKEN=<pat> -e OWNER=<owner> -e REPO=<repo> do-actions-runner

# Organization-level
docker run -e TOKEN=<pat> -e ORG=<org> do-actions-runner
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TOKEN` | Yes | GitHub PAT (`repo` or `admin:org` scope) |
| `OWNER` | For repo | Repository owner |
| `REPO` | For repo | Repository name |
| `ORG` | For org | Organization name |
| `NAME` | No | Custom runner name (default: hostname) |
| `RUNNERS_PER_INSTANCE` | No | Number of runner processes per container (default: 1) |

## Guidelines for AI Agents

### Critical Constraints
- Preserve `set -eEuo pipefail` in entrypoint.sh
- Keep cleanup trap for SIGTERM - ensures runner deregistration
- Runner must run as `actions` user, not root
- Never log/expose `TOKEN` or `RUNNER_TOKEN`
- Runner version is fetched dynamically - don't hardcode

### Python Logging Standards
Use structured logging with proper levels - never use `print()`:

```python
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

log.debug("Verbose internal state")     # Routine operations
log.info("Operation completed")         # Key events
log.warning("Non-critical issue")       # Recoverable problems
log.error("Operation failed: %s", err)  # Errors that need attention
log.exception("Crash details")          # Errors with stack trace
```

### Testing Changes
- Test both repo-level and org-level configurations
- Verify runner appears in GitHub Settings > Actions > Runners
- Test graceful shutdown confirms deregistration

See `docs/` for detailed documentation.
