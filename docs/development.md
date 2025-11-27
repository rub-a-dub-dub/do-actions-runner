# Development Guide

## Local Development

### Building

```bash
docker build -t do-actions-runner .
```

### Running Locally

Repository-level runner:
```bash
docker run \
  -e TOKEN=<github_personal_access_token> \
  -e OWNER=<repository_owner> \
  -e REPO=<repository_name> \
  do-actions-runner
```

Organization-level runner:
```bash
docker run \
  -e TOKEN=<github_personal_access_token> \
  -e ORG=<organization_name> \
  do-actions-runner
```

### Testing Graceful Shutdown

```bash
# Start container
docker run --name test-runner -e TOKEN=... -e OWNER=... -e REPO=... do-actions-runner

# In another terminal, send SIGTERM
docker stop test-runner

# Verify runner is removed from GitHub Settings > Actions > Runners
```

## Modifying the Dockerfile

### Adding Packages

Packages should be added in the appropriate layer:
- System packages: Add to the `apt-get install` section
- Node.js packages: Add to the `npm install -g` section

### Updating Runner Version

The runner version is fetched automatically from GitHub API at build time. To pin a specific version, modify:

```dockerfile
RUNNER_VERSION="2.xxx.x"  # Instead of API call
```

## Modifying entrypoint.sh

### Shell Script Standards

- Keep `set -eEuo pipefail` - ensures fail-fast behavior
- Use `${VAR:-}` for optional variables to avoid unbound variable errors
- Always maintain the cleanup trap for proper deregistration

### Environment Variable Handling

```bash
# Required variable - will fail if not set
if [ -z "${TOKEN:-}" ]; then
  echo "TOKEN is required"
  exit 1
fi

# Optional variable with default
NAME="${NAME:-$(hostname)}"
```

## Security Notes

- `TOKEN` is a GitHub Personal Access Token - never log it
- `RUNNER_TOKEN` is short-lived and scoped to registration only
- The runner process runs as non-root `actions` user
- Secrets in deploy.template.yaml use `type: SECRET`
