# Autoscaler

The autoscaler automatically adjusts the number of runner instances based on GitHub Actions job demand (queued + in_progress jobs).

## Algorithm Overview

```
                    ┌─────────────────────┐
                    │   Poll GitHub API   │
                    │  (job demand count) │
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
     │  demand > current │     │     │  demand < current │
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

**Job Demand** = filtered queued jobs + filtered in_progress jobs. This prevents scaling down while jobs are still running.

**Runner Capacity** = current_instances × RUNNERS_PER_INSTANCE. When running multiple runners per container, scaling decisions compare job demand against total runner capacity rather than instance count.

### Job Filtering

Not all jobs in your org/repo are relevant to this runner group. The autoscaler filters jobs:

- **Queued jobs**: Only counts jobs with `self-hosted` in their labels (jobs targeting self-hosted runners)
- **In-progress jobs**: Only counts jobs where `runner_name` matches `RUNNER_NAME_PREFIX`

This prevents the autoscaler from scaling based on GitHub-hosted runner jobs or jobs running on other self-hosted runner groups.

## Anti-Thrashing Mechanisms

### 1. Hysteresis Thresholds

Scale-up and scale-down use different thresholds to create a dead zone:

- **Scale-up**: `job_demand > runner_capacity × SCALE_UP_THRESHOLD`
- **Scale-down**: `job_demand < runner_capacity × SCALE_DOWN_THRESHOLD`

Example with defaults (1.5x up, 0.25x down, 1 runner per instance):
- 2 instances (capacity=2), 4 job demand → scale up (4 > 3.0)
- 2 instances (capacity=2), 1 job demand → stable (1 is not > 3.0 and not < 0.5)
- 2 instances (capacity=2), 0 job demand → scale down (0 < 0.5)

Example with `RUNNERS_PER_INSTANCE=2`:
- 2 instances (capacity=4), 5 job demand → stable (5 is not > 6.0 and not < 1.0)
- 2 instances (capacity=4), 8 job demand → scale up (8 > 6.0)

### 2. Time-Decay Stabilization

Scaling decisions use a time-weighted breach score instead of simple consecutive counts. Each threshold breach is recorded with a timestamp and contributes to a cumulative score using exponential decay:

```
breach_score = Σ 0.5^(age_seconds / DECAY_HALF_LIFE_SECONDS)
```

Scaling triggers when `breach_score >= BREACH_THRESHOLD`.

**How it works:**
- Each breach starts with weight ~1.0
- Weight halves every `DECAY_HALF_LIFE_SECONDS` (default: 30s)
- Breaches older than `STABILIZATION_WINDOW_MINUTES` (default: 3min) are pruned
- Default `BREACH_THRESHOLD` is 2.0 (roughly 2+ recent breaches needed)

**Benefits over consecutive counts:**
- More responsive to sustained conditions
- Brief fluctuations don't completely reset progress
- Natural smoothing of noisy data

### 3. Proportional Scaling with Step Maximums

Scaling is proportional to the demand gap, with configurable maximum step sizes. When `RUNNERS_PER_INSTANCE > 1`, the deficit/excess is calculated against runner capacity, then converted to instances:

**Scale-up formula:**
```
runner_capacity = current_instances × RUNNERS_PER_INSTANCE
runner_deficit = demand - runner_capacity
instance_step = min(max(int((runner_deficit × SCALE_UP_PROPORTION) / RUNNERS_PER_INSTANCE + 0.5), 1), SCALE_UP_STEP)
new_count = min(current + instance_step, MAX_INSTANCES)
```

**Scale-down formula:**
```
runner_capacity = current_instances × RUNNERS_PER_INSTANCE
excess_capacity = runner_capacity - demand
instance_step = min(max(int((excess_capacity × SCALE_DOWN_PROPORTION) / RUNNERS_PER_INSTANCE + 0.5), 1), SCALE_DOWN_STEP)
new_count = max(current - instance_step, MIN_INSTANCES)
```

This approach:
- Scales faster when the gap is large (proportional to deficit/excess)
- Never adds/removes more than `SCALE_UP_STEP`/`SCALE_DOWN_STEP` at once (safety cap)
- Always changes by at least 1 instance when triggered (minimum step)
- Allows asymmetric behavior (fast scale-up, conservative scale-down)

### 4. Independent Cooldown Periods

After scaling, further scaling **in the same direction** is blocked. Each direction has its own cooldown:

- **Scale-up cooldown**: Default 60 seconds
- **Scale-down cooldown**: Default 180 seconds (longer to let new instances stabilize)

**Important**: Cooldowns are independent. Scale-up during a scale-down cooldown is allowed (and vice versa). This enables quick response to sudden load spikes even after scaling down.

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
| `RUNNER_NAME_PREFIX` | `""` | Prefix to match runner names (empty = count all self-hosted) |
| `RUNNERS_PER_INSTANCE` | `1` | Number of runner processes per container instance |
| `MIN_INSTANCES` | `1` | Minimum instance count (DO App Platform requires >= 1) |
| `MAX_INSTANCES` | `5` | Maximum instance count |
| `POLL_INTERVAL` | `60` | Seconds between polls |
| `SCALE_UP_THRESHOLD` | `1.5` | Multiplier for scale-up trigger |
| `SCALE_DOWN_THRESHOLD` | `0.25` | Multiplier for scale-down trigger |
| `SCALE_UP_COOLDOWN` | `60` | Seconds to wait after scale-up |
| `SCALE_DOWN_COOLDOWN` | `180` | Seconds to wait after scale-down |
| `SCALE_UP_STEP` | `2` | Maximum instances to add when scaling up |
| `SCALE_DOWN_STEP` | `1` | Maximum instances to remove when scaling down |
| `SCALE_UP_PROPORTION` | `0.5` | Fraction of deficit to scale up by |
| `SCALE_DOWN_PROPORTION` | `0.5` | Fraction of excess to scale down by |
| `STABILIZATION_WINDOW_MINUTES` | `3` | Time window for breach history |
| `DECAY_HALF_LIFE_SECONDS` | `30` | Half-life for breach score decay |
| `BREACH_THRESHOLD` | `2.0` | Score needed to trigger scaling |

## Scaling Behavior Examples

### Burst of Jobs

With defaults (`SCALE_UP_STEP=2`, `BREACH_THRESHOLD=2.0`, `POLL_INTERVAL=60`):

```
t=0:   queued=0, instances=1 → stable
t=60:  queued=5, instances=1 → scale-up breach recorded (score ~1.0)
t=120: queued=8, instances=1 → scale-up breach recorded (score ~2.0) → scale to 3
t=180: queued=6, instances=3 → scale-up blocked (cooldown)
t=240: queued=5, instances=3 → cooldown expired, stable (5 < 4.5)
...
```

### Jobs Complete

With defaults (`SCALE_DOWN_STEP=1`, `BREACH_THRESHOLD=2.0`):

```
t=0:   queued=3, instances=3 → stable
t=60:  queued=1, instances=3 → stable (1 > 0.75)
t=120: queued=0, instances=3 → scale-down breach recorded (score ~1.0)
t=180: queued=0, instances=3 → scale-down breach recorded (score ~2.0) → scale to 2
t=360: queued=0, instances=2 → cooldown expired, breach score ~2.0 → scale to 1
```

### Rapid Response to Load Spike

Independent cooldowns allow quick response to load changes:

```
t=0:   instances=3, just scaled down
t=30:  queued=10 → scale-up allowed (down cooldown doesn't block up)
       breach recorded (score ~1.0)
t=90:  queued=12 → breach recorded (score ~2.0) → scale to 5
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
