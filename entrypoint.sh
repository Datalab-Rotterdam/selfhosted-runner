#!/usr/bin/env bash
set -euo pipefail

: "${REPO_URL:?REPO_URL required}"
# The coordinator copies a short-lived registration token into TOKEN_FILE, so neither the PAT nor
# the token is in this container's environment (jobs could read that via sudo and /proc/1/environ).
# GITHUB_PAT: standalone use; the runner then also deregisters itself on stop.
TOKEN_FILE=/home/runner/.registration-token
if [[ ! -s "$TOKEN_FILE" && -z "${GITHUB_PAT:-}" ]]; then
  echo "ERROR: GITHUB_PAT or a registration token in $TOKEN_FILE required" >&2
  exit 1
fi

GITHUB_API_URL="${GITHUB_API_URL:-https://api.github.com}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
RUNNER_LABELS="${RUNNER_LABELS:-}"
RUNNER_GROUP="${RUNNER_GROUP:-}"
RUNNER_EPHEMERAL="${RUNNER_EPHEMERAL:-false}"

cd /home/runner/actions-runner

# https://github.com/owner/repo -> repos/owner/repo ; https://github.com/org -> orgs/org
path="${REPO_URL#*://*/}"
path="${path%/}"
if [[ "$path" == */* ]]; then
  scope="repos/${path}"
else
  scope="orgs/${path}"
fi

token() { # $1 = registration-token | remove-token
  local out code
  out=$(curl -sS -w '\n%{http_code}' -X POST \
    -H "Authorization: Bearer ${GITHUB_PAT}" \
    -H "Accept: application/vnd.github+json" \
    "${GITHUB_API_URL}/${scope}/actions/runners/$1") || { echo "ERROR: cannot reach ${GITHUB_API_URL}" >&2; return 1; }
  code="${out##*$'\n'}"
  out="${out%$'\n'*}"
  if [[ "$code" != 2* ]]; then
    echo "ERROR: GitHub API ${code} for ${scope}: $(jq -r '.message // empty' <<<"$out")" >&2
    return 1
  fi
  jq -r .token <<<"$out"
}

# drop stale registration from a previous run of this container
rm -f .runner .credentials .credentials_rsaparams

if [[ -s "$TOKEN_FILE" ]]; then
  reg_token="$(<"$TOKEN_FILE")"
  rm -f "$TOKEN_FILE"
else
  reg_token="$(token registration-token)"
fi

# docker: dind gives the runner a workspace volume shared with its daemon (so `docker run -v
# $GITHUB_WORKSPACE:...` resolves) and client certs that appear once the daemon has started.
sudo chown runner:runner _work
# `devices: [/dev/kvm]`: the node keeps the host's group, which the runner user is not in. This changes
# only the container's own copy of the node.
[[ -c /dev/kvm ]] && sudo chmod 0666 /dev/kvm
if [[ -n "${DOCKER_CERT_PATH:-}" ]]; then
  for _ in $(seq 60); do [[ -s "${DOCKER_CERT_PATH}/ca.pem" ]] && break; sleep 1; done
  [[ -s "${DOCKER_CERT_PATH}/ca.pem" ]] || echo "WARN: no docker client certs in ${DOCKER_CERT_PATH}" >&2
fi

args=(--url "$REPO_URL" --token "$reg_token" --name "$RUNNER_NAME"
      --unattended --replace)
[[ -n "$RUNNER_LABELS" ]] && args+=(--labels "$RUNNER_LABELS")
[[ -n "$RUNNER_GROUP" ]] && args+=(--runnergroup "$RUNNER_GROUP")
[[ "$RUNNER_EPHEMERAL" == "true" ]] && args+=(--ephemeral)

./config.sh "${args[@]}"

# Secrets must not reach jobs: steps inherit this process's environment.
pat="${GITHUB_PAT:-}"
unset GITHUB_PAT reg_token

cleanup() {
  echo "Deregistering runner ${RUNNER_NAME}"
  kill -TERM "$child" 2>/dev/null || true
  wait "$child" 2>/dev/null || true
  # without a PAT the coordinator deregisters the runner through the API instead
  if [[ -n "$pat" ]]; then
    GITHUB_PAT="$pat" ./config.sh remove --token "$(GITHUB_PAT="$pat" token remove-token)" || true
  fi
}
trap cleanup TERM INT

./run.sh &
child=$!
wait "$child"
status=$?
trap - TERM INT
exit "$status"
