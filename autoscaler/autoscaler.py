#!/usr/bin/env python3
"""
GitHub Actions Runner Autoscaler for DigitalOcean App Platform.

Polls GitHub API for queued jobs and scales runner workers accordingly.
"""

import os
import sys
import time

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
JOBS_PER_RUNNER = int(os.environ.get("JOBS_PER_RUNNER", "1"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))

GITHUB_API = "https://api.github.com"
DO_API = "https://api.digitalocean.com/v2"


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


def calculate_desired_instances(queued_jobs: int) -> int:
    """Calculate desired instance count based on queue depth."""
    desired = max(MIN_INSTANCES, (queued_jobs + JOBS_PER_RUNNER - 1) // JOBS_PER_RUNNER)
    return min(desired, MAX_INSTANCES)


def main():
    """Main autoscaler loop (runs continuously)."""
    print("Starting GitHub Actions Runner Autoscaler")
    print(f"  Worker: {WORKER_NAME}")
    print(f"  Min instances: {MIN_INSTANCES}")
    print(f"  Max instances: {MAX_INSTANCES}")
    print(f"  Poll interval: {POLL_INTERVAL}s")

    if not all([GITHUB_TOKEN, DO_API_TOKEN, APP_ID]):
        print("ERROR: GITHUB_TOKEN, DO_API_TOKEN, and APP_ID are required")
        sys.exit(1)

    while True:
        try:
            queued = get_queued_jobs()
            current = get_current_instance_count()
            desired = calculate_desired_instances(queued)

            if desired != current:
                print(f"Scaling {WORKER_NAME}: {current} -> {desired}")
                scale_worker(desired)
            else:
                print(f"No scaling needed ({current} instances)")

        except requests.RequestException as e:
            print(f"API error: {e}")
        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
