#!/usr/bin/env bash
set -eEuo pipefail

# Increase file descriptor limit for actions that extract many files
ulimit -n 65536 2>/dev/null || ulimit -n 32768 2>/dev/null || ulimit -n 16384 2>/dev/null || true
echo "File descriptor limit: $(ulimit -n)"

if [ -z "${TOKEN:-}" ]
then
  echo "TOKEN is required"
  exit 1
fi

if [ -n "${ORG:-}" ]
then
  API_PATH=orgs/${ORG}
  CONFIG_PATH=${ORG}
elif [ -n "${OWNER:-}" ] && [ -n "${REPO:-}" ]
then
  API_PATH=repos/${OWNER}/${REPO}
  CONFIG_PATH=${OWNER}/${REPO}
else
  echo "[ORG] or [OWNER and REPO] is required"
  exit 1
fi

RUNNER_TOKEN=$(curl -s -X POST -H "authorization: token ${TOKEN}" "https://api.github.com/${API_PATH}/actions/runners/registration-token" | jq -r .token)

cleanup() {
  ./config.sh remove --token "${RUNNER_TOKEN}"
}

./config.sh \
  --url "https://github.com/${CONFIG_PATH}" \
  --token "${RUNNER_TOKEN}" \
  --name "${NAME:+${NAME}-}$(hostname)" \
  --unattended

trap 'cleanup' SIGTERM

./run.sh "$@" &

wait $!
