# Architecture

## Components

### Dockerfile

Ubuntu-based image that:
- Downloads the latest GitHub Actions runner from official releases via GitHub API
- Installs common CI/CD tooling from GitHub's `actions/runner-images` toolset
- Installs Node.js LTS and build tools (grunt, gulp, typescript, webpack, etc.)
- Creates non-root `actions` user for running the agent

Key layers:
1. Base Ubuntu + apt packages from runner-images toolset
2. GitHub Actions runner binary (latest version)
3. Git from ppa:git-core/ppa
4. Node.js LTS via n-install

### entrypoint.sh

Runner lifecycle management script:

1. **Validation** - Checks required environment variables (`TOKEN` + either `ORG` or `OWNER`/`REPO`)
2. **Registration** - Obtains short-lived registration token via GitHub API
3. **Configuration** - Runs `config.sh` to register runner with GitHub
4. **Execution** - Starts `run.sh` in background
5. **Cleanup** - SIGTERM trap calls `config.sh remove` to deregister

## Data Flow

```
Container Start
    │
    ▼
Validate ENV vars ──► Exit if missing
    │
    ▼
Get registration token from GitHub API
    │
    ▼
Configure runner (config.sh)
    │
    ▼
Start runner (run.sh) ◄──► GitHub Actions jobs
    │
    ▼
SIGTERM received
    │
    ▼
Cleanup: deregister runner
```

## DigitalOcean App Platform Integration

- Deploy template in `.do/deploy.template.yaml`
- Runs as a worker component (not a service)
- Supports horizontal scaling - each instance auto-registers
- SIGTERM handling ensures clean deregistration on scale-down
