#!/usr/bin/env python3
"""
GitHub Actions Runner Autoscaler for DigitalOcean App Platform.

Polls GitHub API for queued jobs and scales runner workers accordingly.
Includes cooldown periods, hysteresis thresholds, and stabilization windows
to prevent thrashing.
"""

import os
import sys
import time
from dataclasses import dataclass, field

import requests

# Configuration from environment
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
DO_API_TOKEN = os.environ.get("DO_API_TOKEN")
APP_ID = os.environ.get("APP_ID")

# Optional: org-level or repo-level
ORG = os.environ.get("ORG")
OWNER = os.environ.get("OWNER")
REPO = os.environ.get("REPO")

# Scaling configuration
WORKER_NAME = os.environ.get("WORKER_NAME", "runner")
MIN_INSTANCES = int(os.environ.get("MIN_INSTANCES", "1"))
MAX_INSTANCES = int(os.environ.get("MAX_INSTANCES", "5"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))

# Cooldown configuration (anti-thrashing)
SCALE_UP_COOLDOWN = int(os.environ.get("SCALE_UP_COOLDOWN", "60"))  # seconds
SCALE_DOWN_COOLDOWN = int(os.environ.get("SCALE_DOWN_COOLDOWN", "180"))  # seconds

# Hysteresis thresholds
SCALE_UP_THRESHOLD = float(os.environ.get("SCALE_UP_THRESHOLD", "1.5"))
SCALE_DOWN_THRESHOLD = float(os.environ.get("SCALE_DOWN_THRESHOLD", "0.25"))

# Stabilization window (consecutive readings required)
STABILIZATION_WINDOW = int(os.environ.get("STABILIZATION_WINDOW", "3"))

GITHUB_API = "https://api.github.com"
DO_API = "https://api.digitalocean.com/v2"


@dataclass
class ScalingState:
    """Tracks scaling state for cooldown and stabilization."""

    last_scale_time: float = 0
    last_scale_direction: str = ""  # "up" or "down"
    consecutive_scale_up_readings: int = 0
    consecutive_scale_down_readings: int = 0


def get_queued_jobs() -> int:
    """Get count of queued workflow jobs from GitHub."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    if ORG:
        # Org-level: list all queued runs across the org
        url = f"{GITHUB_API}/orgs/{ORG}/actions/runs?status=queued&per_page=100"
    elif OWNER and REPO:
        # Repo-level
        url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runs?status=queued&per_page=100"
    else:
        print("ERROR: ORG or (OWNER and REPO) must be set")
        sys.exit(1)

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    queued_count = data.get("total_count", 0)
    print(f"Queued jobs: {queued_count}")
    return queued_count


def get_current_instance_count() -> int:
    """Get current instance count for the worker from DO App spec."""
    headers = {"Authorization": f"Bearer {DO_API_TOKEN}"}
    url = f"{DO_API}/apps/{APP_ID}"

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    app = resp.json().get("app", {})

    for worker in app.get("spec", {}).get("workers", []):
        if worker.get("name") == WORKER_NAME:
            count = worker.get("instance_count", 1)
            print(f"Current {WORKER_NAME} instances: {count}")
            return count

    print(f"WARNING: Worker '{WORKER_NAME}' not found in app spec")
    return 1


def scale_worker(desired_count: int) -> None:
    """Update the worker instance count via DO API."""
    headers = {
        "Authorization": f"Bearer {DO_API_TOKEN}",
        "Content-Type": "application/json",
    }

    # Get current app spec
    resp = requests.get(f"{DO_API}/apps/{APP_ID}", headers=headers)
    resp.raise_for_status()
    app = resp.json().get("app", {})
    spec = app.get("spec", {})

    # Update the target worker's instance count
    updated = False
    for worker in spec.get("workers", []):
        if worker.get("name") == WORKER_NAME:
            worker["instance_count"] = desired_count
            updated = True
            break

    if not updated:
        print(f"ERROR: Worker '{WORKER_NAME}' not found")
        return

    # Apply updated spec
    resp = requests.put(
        f"{DO_API}/apps/{APP_ID}",
        headers=headers,
        json={"spec": spec},
    )
    resp.raise_for_status()
    print(f"Scaled {WORKER_NAME} to {desired_count} instances")


def should_scale_up(queued: int, current: int) -> bool:
    """Check if scale-up threshold is met."""
    threshold = current * SCALE_UP_THRESHOLD
    return queued > threshold


def should_scale_down(queued: int, current: int) -> bool:
    """Check if scale-down threshold is met."""
    threshold = current * SCALE_DOWN_THRESHOLD
    return queued < threshold


def is_cooldown_active(state: ScalingState, direction: str) -> bool:
    """Check if cooldown period is still active for the given direction."""
    if state.last_scale_time == 0:
        return False

    elapsed = time.time() - state.last_scale_time

    if direction == "up":
        return elapsed < SCALE_UP_COOLDOWN
    else:  # down
        return elapsed < SCALE_DOWN_COOLDOWN


def evaluate_scaling(
    queued: int, current: int, state: ScalingState
) -> tuple[str, int]:
    """
    Evaluate scaling decision based on thresholds, stabilization, and cooldown.

    Returns:
        tuple of (action, new_count) where action is "up", "down", or "none"
    """
    # Check scale-up condition
    if should_scale_up(queued, current):
        state.consecutive_scale_up_readings += 1
        state.consecutive_scale_down_readings = 0
        print(
            f"Scale-up condition met ({queued} > {current * SCALE_UP_THRESHOLD:.1f}), "
            f"readings: {state.consecutive_scale_up_readings}/{STABILIZATION_WINDOW}"
        )

        if state.consecutive_scale_up_readings >= STABILIZATION_WINDOW:
            if is_cooldown_active(state, "up"):
                remaining = SCALE_UP_COOLDOWN - (time.time() - state.last_scale_time)
                print(f"Scale-up blocked by cooldown ({remaining:.0f}s remaining)")
                return ("none", current)

            new_count = min(current + 1, MAX_INSTANCES)
            if new_count > current:
                return ("up", new_count)
            else:
                print(f"Already at MAX_INSTANCES ({MAX_INSTANCES})")

    # Check scale-down condition
    elif should_scale_down(queued, current):
        state.consecutive_scale_down_readings += 1
        state.consecutive_scale_up_readings = 0
        print(
            f"Scale-down condition met ({queued} < {current * SCALE_DOWN_THRESHOLD:.1f}), "
            f"readings: {state.consecutive_scale_down_readings}/{STABILIZATION_WINDOW}"
        )

        if state.consecutive_scale_down_readings >= STABILIZATION_WINDOW:
            if is_cooldown_active(state, "down"):
                remaining = SCALE_DOWN_COOLDOWN - (time.time() - state.last_scale_time)
                print(f"Scale-down blocked by cooldown ({remaining:.0f}s remaining)")
                return ("none", current)

            new_count = max(current - 1, MIN_INSTANCES)
            if new_count < current:
                return ("down", new_count)
            else:
                print(f"Already at MIN_INSTANCES ({MIN_INSTANCES})")

    # No scaling condition met - reset counters
    else:
        state.consecutive_scale_up_readings = 0
        state.consecutive_scale_down_readings = 0
        print(f"Queue stable (no scaling thresholds met)")

    return ("none", current)


def get_runners() -> list[dict]:
    """Get list of all registered runners from GitHub."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    if ORG:
        url = f"{GITHUB_API}/orgs/{ORG}/actions/runners?per_page=100"
    elif OWNER and REPO:
        url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runners?per_page=100"
    else:
        return []

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    return data.get("runners", [])


def delete_runner(runner_id: int) -> bool:
    """Delete a runner by ID from GitHub."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    if ORG:
        url = f"{GITHUB_API}/orgs/{ORG}/actions/runners/{runner_id}"
    elif OWNER and REPO:
        url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runners/{runner_id}"
    else:
        return False

    resp = requests.delete(url, headers=headers)
    return resp.status_code == 204


def cleanup_dead_runners() -> int:
    """Remove offline runners that are not busy.

    Returns:
        Number of runners deleted.
    """
    try:
        runners = get_runners()
    except requests.RequestException as e:
        print(f"Failed to get runners: {e}")
        return 0

    deleted = 0
    for runner in runners:
        status = runner.get("status", "")
        busy = runner.get("busy", False)
        name = runner.get("name", "unknown")
        runner_id = runner.get("id")

        # Only delete offline runners that are not busy
        if status == "offline" and not busy and runner_id:
            print(f"Removing dead runner: {name} (ID: {runner_id})")
            try:
                if delete_runner(runner_id):
                    deleted += 1
                    print(f"  Deleted runner {name}")
                else:
                    print(f"  Failed to delete runner {name}")
            except requests.RequestException as e:
                print(f"  Error deleting runner {name}: {e}")

    if deleted > 0:
        print(f"Cleaned up {deleted} dead runner(s)")
    return deleted


def main():
    """Main autoscaler loop (runs continuously)."""
    print("Starting GitHub Actions Runner Autoscaler")
    print(f"  Worker: {WORKER_NAME}")
    print(f"  Min instances: {MIN_INSTANCES}")
    print(f"  Max instances: {MAX_INSTANCES}")
    print(f"  Poll interval: {POLL_INTERVAL}s")
    print(f"  Scale-up threshold: {SCALE_UP_THRESHOLD}x capacity")
    print(f"  Scale-down threshold: {SCALE_DOWN_THRESHOLD}x capacity")
    print(f"  Scale-up cooldown: {SCALE_UP_COOLDOWN}s")
    print(f"  Scale-down cooldown: {SCALE_DOWN_COOLDOWN}s")
    print(f"  Stabilization window: {STABILIZATION_WINDOW} readings")

    if not all([GITHUB_TOKEN, DO_API_TOKEN, APP_ID]):
        print("ERROR: GITHUB_TOKEN, DO_API_TOKEN, and APP_ID are required")
        sys.exit(1)

    state = ScalingState()

    while True:
        try:
            # Clean up any dead runners first
            cleanup_dead_runners()

            queued = get_queued_jobs()
            current = get_current_instance_count()

            action, new_count = evaluate_scaling(queued, current, state)

            if action != "none":
                print(f"Scaling {WORKER_NAME}: {current} -> {new_count}")
                scale_worker(new_count)
                state.last_scale_time = time.time()
                state.last_scale_direction = action
                # Reset counters after scaling
                state.consecutive_scale_up_readings = 0
                state.consecutive_scale_down_readings = 0
            else:
                print(f"No scaling action ({current} instances)")

        except requests.RequestException as e:
            print(f"API error: {e}")
        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
