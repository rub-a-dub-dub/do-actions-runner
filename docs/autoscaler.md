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

### 3. Step-Based Scaling

Scaling uses asymmetric step sizes to handle bursts efficiently while scaling down conservatively:

- **Scale-up**: Add `SCALE_UP_STEP` instances (default: +2)
- **Scale-down**: Remove `SCALE_DOWN_STEP` instances (default: -1)

This allows the system to react quickly to job bursts while scaling down gradually as load decreases.

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
| `MIN_INSTANCES` | `1` | Minimum instance count |
| `MAX_INSTANCES` | `5` | Maximum instance count |
| `POLL_INTERVAL` | `60` | Seconds between polls |
| `SCALE_UP_THRESHOLD` | `1.5` | Multiplier for scale-up trigger |
| `SCALE_DOWN_THRESHOLD` | `0.25` | Multiplier for scale-down trigger |
| `SCALE_UP_COOLDOWN` | `60` | Seconds to wait after scale-up |
| `SCALE_DOWN_COOLDOWN` | `180` | Seconds to wait after scale-down |
| `SCALE_UP_STEP` | `2` | Instances to add when scaling up |
| `SCALE_DOWN_STEP` | `1` | Instances to remove when scaling down |
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
