# Autoscaler

The autoscaler automatically adjusts the number of runner instances based on GitHub Actions job demand and runner availability.

## Ephemeral Runner Model

Runners use GitHub's `--ephemeral` flag, meaning each runner:
1. Picks up exactly ONE job
2. Automatically deregisters and exits after the job completes
3. Container restarts and registers a fresh runner

This model enables **safe scale-down**: since runners exit after completing jobs, App Platform can safely terminate idle containers without interrupting running jobs.

## Algorithm Overview

```
                    ┌─────────────────────┐
                    │   Poll GitHub API   │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
        Queued Jobs      Online Runners    Idle Runners
              │                │                │
              └────────────────┼────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Evaluate Scaling   │
                    │    (3 priorities)   │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │   Update DO App     │
                    │   worker count      │
                    └─────────────────────┘
```

### Scaling Priorities

1. **Maintain Minimum Capacity** (bypass cooldown)
   - If `online_runners < MIN_INSTANCES × RUNNERS_PER_INSTANCE`, scale up immediately
   - Ensures minimum runner availability

2. **Scale Up for Queued Jobs**
   - If `queued_jobs > 0` and not at max capacity, scale up
   - Proportional to queue size: `instances_to_add = ceil(queued_jobs / RUNNERS_PER_INSTANCE × SCALE_UP_PROPORTION)`
   - Capped by `SCALE_UP_STEP`
   - Subject to `SCALE_UP_COOLDOWN`

3. **Scale Down When Idle**
   - If `queued_jobs == 0` AND `idle_runners > MIN_INSTANCES × RUNNERS_PER_INSTANCE`, scale down by 1
   - Only considers **idle** runners (not busy), so busy runners are never terminated
   - Subject to `SCALE_DOWN_COOLDOWN`

### Job and Runner Filtering

**Queued Jobs**: Only counts jobs with `self-hosted` in their labels (jobs targeting self-hosted runners).

**Online/Idle Runners**: Only counts runners where `runner_name` matches `RUNNER_NAME_PREFIX`. This prevents interference with other runner groups.

## Anti-Thrashing Mechanisms

### 1. Independent Cooldown Periods

After scaling, further scaling **in the same direction** is blocked:

- **Scale-up cooldown**: Default 60 seconds
- **Scale-down cooldown**: Default 180 seconds (longer to let ephemeral runners complete)

Cooldowns are independent. Scale-up during scale-down cooldown is allowed (and vice versa).

### 2. Proportional Scale-Up

Scale-up is proportional to the queue size but capped:

```
instances_to_add = min(
    ceil(queued_jobs / RUNNERS_PER_INSTANCE × SCALE_UP_PROPORTION),
    SCALE_UP_STEP
)
```

### 3. Conservative Scale-Down

Scale-down removes only 1 instance at a time, ensuring gradual reduction.

## Dead Runner Cleanup

On each poll cycle, the autoscaler removes "dead" runners:

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
| `RUNNER_NAME_PREFIX` | `""` | Prefix to match runner names (empty = count all self-hosted) |
| `RUNNERS_PER_INSTANCE` | `1` | Number of runner processes per container instance |
| `MIN_INSTANCES` | `1` | Minimum instance count (DO App Platform requires >= 1) |
| `MAX_INSTANCES` | `5` | Maximum instance count |
| `POLL_INTERVAL` | `60` | Seconds between polls |
| `SCALE_UP_COOLDOWN` | `60` | Seconds to wait after scale-up |
| `SCALE_DOWN_COOLDOWN` | `180` | Seconds to wait after scale-down |
| `SCALE_UP_STEP` | `2` | Maximum instances to add when scaling up |
| `SCALE_UP_PROPORTION` | `0.5` | Fraction of queue to scale up by |

## Scaling Behavior Examples

### Burst of Jobs

With defaults (`SCALE_UP_STEP=2`, `POLL_INTERVAL=60`):

```
t=0:   queued=0, idle=1 → stable
t=60:  queued=5, idle=1 → scale up to 3 (5 × 0.5 = 2.5 → +2)
t=120: queued=3, idle=0 → cooldown expired, scale up to 5 (3 × 0.5 = 1.5 → +2)
t=180: queued=2, idle=0 → stable (at max)
```

### Jobs Complete (Ephemeral Exit)

```
t=0:   queued=0, instances=3, idle=0 (all busy) → stable
       [runners complete jobs and exit, App Platform restarts them]
t=60:  queued=0, instances=3, idle=3 → scale down to 2 (idle > min)
t=240: queued=0, instances=2, idle=2 → scale down to 1 (cooldown expired)
t=420: queued=0, instances=1, idle=1 → stable (at min)
```

### Rapid Response to Load Spike

Independent cooldowns allow quick response to load changes:

```
t=0:   instances=1, just scaled down
t=30:  queued=10 → scale up allowed (down cooldown doesn't block up) → scale to 3
t=90:  queued=5 → scale up to 5 (cooldown expired)
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

## Known Limitations

- **Multiple runner apps**: If you run multiple self-hosted runner deployments (each with their own autoscaler), they may over-scale when counting queued jobs. Queued jobs use the `self-hosted` label for filtering, which doesn't distinguish between different runner groups. Each autoscaler will count all queued self-hosted jobs, potentially causing all groups to scale up for the same jobs. Consider using custom labels and updating the filtering logic if you need multiple isolated runner pools.
