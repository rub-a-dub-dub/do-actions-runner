# Autoscaler

The autoscaler automatically adjusts the number of runner instances based on GitHub Actions job queue depth.

## Algorithm Overview

```
                    ┌─────────────────────┐
                    │   Poll GitHub API   │
                    │  (queued job count) │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ Get current instance│
                    │   count from DO     │
                    └──────────┬──────────┘
                               │
               ┌───────────────┼───────────────┐
               │               │               │
     ┌─────────▼─────────┐     │     ┌─────────▼─────────┐
     │  queued > current │     │     │  queued < current │
     │  × SCALE_UP_THRES │     │     │  × SCALE_DOWN_THRES
     └─────────┬─────────┘     │     └─────────┬─────────┘
               │               │               │
               ▼               ▼               ▼
         Scale Up?          Stable        Scale Down?
               │                               │
               └───────────────┬───────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Stabilization &    │
                    │  Cooldown checks    │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │   Update DO App     │
                    │   worker count      │
                    └─────────────────────┘
```

## Anti-Thrashing Mechanisms

### 1. Hysteresis Thresholds

Scale-up and scale-down use different thresholds to create a dead zone:

- **Scale-up**: `queued_jobs > current_instances × SCALE_UP_THRESHOLD`
- **Scale-down**: `queued_jobs < current_instances × SCALE_DOWN_THRESHOLD`

Example with defaults (1.5x up, 0.25x down):
- 2 instances, 4 queued jobs → scale up (4 > 3.0)
- 2 instances, 1 queued job → stable (1 is not > 3.0 and not < 0.5)
- 2 instances, 0 queued jobs → scale down (0 < 0.5)

### 2. Stabilization Window

Scaling only occurs after N consecutive readings confirm the condition:

```
Reading 1: queued=5 → Scale-up condition met (1/3)
Reading 2: queued=6 → Scale-up condition met (2/3)
Reading 3: queued=4 → Scale-up condition met (3/3) → SCALE UP
```

If the condition isn't met for a reading, the counter resets:

```
Reading 1: queued=5 → Scale-up condition met (1/3)
Reading 2: queued=1 → Stable (counters reset to 0)
Reading 3: queued=5 → Scale-up condition met (1/3)
```

### 3. Cooldown Periods

After scaling, further scaling in the same direction is blocked:

- **Scale-up cooldown**: Default 60 seconds
- **Scale-down cooldown**: Default 180 seconds (longer to let new instances stabilize)

## Dead Runner Cleanup

On each poll cycle, the autoscaler also removes "dead" runners:

1. List all registered runners via GitHub API
2. Find runners with `status: offline` and `busy: false`
3. Delete them via `DELETE /repos/{owner}/{repo}/actions/runners/{id}`

This handles runners that crashed without deregistering.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | required | PAT with `repo` or `admin:org` scope |
| `DO_API_TOKEN` | required | DigitalOcean API token |
| `APP_ID` | required | DO App Platform app ID |
| `OWNER` | for repo | Repository owner |
| `REPO` | for repo | Repository name |
| `ORG` | for org | Organization name |
| `WORKER_NAME` | `runner` | Name of worker component to scale |
| `MIN_INSTANCES` | `1` | Minimum instance count |
| `MAX_INSTANCES` | `5` | Maximum instance count |
| `POLL_INTERVAL` | `60` | Seconds between polls |
| `SCALE_UP_THRESHOLD` | `1.5` | Multiplier for scale-up trigger |
| `SCALE_DOWN_THRESHOLD` | `0.25` | Multiplier for scale-down trigger |
| `SCALE_UP_COOLDOWN` | `60` | Seconds to wait after scale-up |
| `SCALE_DOWN_COOLDOWN` | `180` | Seconds to wait after scale-down |
| `STABILIZATION_WINDOW` | `3` | Consecutive readings required |

## Scaling Behavior Examples

### Burst of Jobs

```
t=0:   queued=0, instances=1 → stable
t=60:  queued=5, instances=1 → scale-up condition (1/3)
t=120: queued=8, instances=1 → scale-up condition (2/3)
t=180: queued=6, instances=1 → scale-up condition (3/3) → scale to 2
t=240: queued=6, instances=2 → scale-up blocked (cooldown)
t=300: queued=5, instances=2 → scale-up condition (1/3)
...
```

### Jobs Complete

```
t=0:   queued=3, instances=3 → stable
t=60:  queued=1, instances=3 → stable (1 > 0.75)
t=120: queued=0, instances=3 → scale-down condition (1/3)
t=180: queued=0, instances=3 → scale-down condition (2/3)
t=240: queued=0, instances=3 → scale-down condition (3/3) → scale to 2
t=420: queued=0, instances=2 → scale-down condition (3/3) → scale to 1
```

## Local Testing

```bash
cd autoscaler
uv run pytest -v
```

Run the autoscaler locally (will fail API calls without real tokens):

```bash
docker build -t autoscaler -f autoscaler/Dockerfile .
docker run -e GITHUB_TOKEN=... -e DO_API_TOKEN=... -e APP_ID=... \
           -e OWNER=... -e REPO=... autoscaler
```
