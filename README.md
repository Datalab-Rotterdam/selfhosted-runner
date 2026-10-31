# selfhosted-runner

Run GitHub Actions self-hosted runners in Docker, for one repo, many repos or a whole org.
Describe what you want in a YAML file; a small **coordinator** container watches it and starts, stops
and scales runner containers to match, optionally spawning runners only while jobs are queued.

- Config in `runners.yaml`, live-reloaded on edit
- Multiple repos or org-level runners, N replicas each
- Autoscale on pending jobs (ephemeral runners, scale to zero)
- Runners register themselves from a PAT (no expiring tokens to paste) and deregister on stop
- Multi-arch images (amd64/arm64) on GHCR

## Quick start

You need Docker (with Compose) on the machine that will run the jobs.

**1. Create a PAT**

- Repo runners: fine-grained PAT with **Administration: read & write** on the repo(s) (add **Actions: read** for autoscale), or a classic PAT with `repo`.
- Org runners: fine-grained PAT with org **Self-hosted runners: read & write**, or a classic PAT with `admin:org`.
- Step-by-step token setup per example: [examples/README.md](examples/README.md#creating-the-pat).

**2. Make a folder with three files** (or copy one from [examples/](examples), e.g. [examples/single-repo](examples/single-repo))

`docker-compose.yml`
```yaml
services:
  coordinator:
    image: ghcr.io/datalab-rotterdam/selfhosted-runner-coordinator:latest
    restart: unless-stopped
    env_file: .env
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./:/config:ro   # mount the folder, not the file: editors that save via rename would otherwise go unseen
```

`runners.yaml`
```yaml
image: ghcr.io/datalab-rotterdam/selfhosted-runner:latest
repos:
  - url: https://github.com/YOUR-ORG/YOUR-REPO
    pat_env: GH_PAT
    replicas: 2
```

`.env`
```
GH_PAT=ghp_xxx
```

**3. Start**

```sh
docker compose up -d
docker compose logs -f coordinator
```

**4. Verify.** Runners show up under the repo's **Settings → Actions → Runners**. In a workflow use:

```yaml
runs-on: self-hosted
```

**5. Change anything** by editing `runners.yaml` (add repos, change `replicas`, labels...). Changes apply
within seconds; changed runners are recreated, removed ones deregister.

**Stop everything:**
```sh
docker compose run --rm coordinator --down && docker compose down
```
(`docker compose stop` alone stops only the coordinator; runners keep running.)

If the GHCR packages are private, `docker login ghcr.io` first or make them public.

## Examples

| folder | shows |
|---|---|
| [examples/single-repo](examples/single-repo) | one repo, 2 fixed runners |
| [examples/multi-repo](examples/multi-repo) | several repos, shared defaults, one autoscaled |
| [examples/org-autoscale](examples/org-autoscale) | org runners scaled by queued jobs |

## Config reference (`runners.yaml`)

| key | scope | meaning |
|---|---|---|
| `image` | top | runner image (default `selfhosted-runner:local`, see [Building locally](#building-locally)) |
| `name_prefix` | top | runner name prefix (default `runner`) |
| `defaults` | top | any per-repo key below, applied to every repo unless overridden |
| `url` | repo | repo URL, or org URL for org-level runners |
| `pat_env` | repo | name of the env var (in `.env`) holding the PAT |
| `replicas` | repo | number of long-lived runners (default 1) |
| `autoscale` | repo | `{min, max}`: ephemeral runners spawned while self-hosted jobs are queued; replaces `replicas` |
| `watch_repos` | repo | org + autoscale only: repo names (without org) to poll for queued jobs |
| `labels` | repo/defaults | extra runner labels |
| `group` | repo/defaults | runner group (org runners) |
| `ephemeral` | repo/defaults | one job per runner, then a fresh one |
| `docker_socket` | repo/defaults | mount the host docker socket into runners so workflows can use `docker` |
| `api_url` | repo/defaults | GitHub Enterprise API base URL |

Coordinator env vars: `POLL_INTERVAL` (default 5s), `SCALE_INTERVAL` (GitHub API poll for autoscale, default 15s),
`COORDINATOR_ID` (default `default`; set a unique value per coordinator if several share one Docker host, otherwise they remove each other's runners and runner names must also differ via `name_prefix`), `STOP_TIMEOUT` (time for a runner to deregister on stop, default 60s), `REGISTER_TIMEOUT` (warn if a runner isn't online by then, default 120s), `MAX_FAILURES` (startup failures before a repo is blocked, default 3).

## Health checks and logs

No extra PAT permissions are needed: listing runners requires the same permission as registering them.

- On start the coordinator uses the PAT to list the repo's/org's runners. A bad or expired PAT, missing
  permission or wrong URL is logged once, e.g. `BLOCKED, not retrying: PAT rejected (invalid or expired)`,
  and no runner containers are started for that entry. Other entries keep working.
- Each runner is confirmed against the GitHub API: `runner-x-1: registered and online`. If it isn't online
  after `REGISTER_TIMEOUT` you get a warning pointing at `docker logs`.
- A runner that exits before registering is logged with its last log lines, retried with backoff, and after
  `MAX_FAILURES` failures in a row the entry is blocked instead of retrying forever. Runners that already
  registered and later stop (crash, finished ephemeral job) are simply restarted.
- Blocked entries stay blocked until you edit `runners.yaml` or recreate the coordinator
  (`docker compose up -d --force-recreate coordinator`, needed after changing a PAT in `.env`).
- Transient problems (network, rate limit, 5xx) are logged and retried.

The coordinator, not Docker, restarts runners (they run without a Docker restart policy), so it survives
reboots only as long as the coordinator itself does (`restart: unless-stopped`).

## Autoscale

With `autoscale: {min, max}` the coordinator polls the GitHub API for queued jobs labelled `self-hosted`
and keeps `clamp(busy + queued, min, max)` ephemeral runners alive. Each runner takes one job and its
container is then removed; idle surplus runners are removed. Job labels aren't matched against your
`labels`, so use one pool per repo. With `min: 0`, the first job waits ~10-20s for a runner to start.

GitHub has no org-wide queue API, so org autoscale polls only the repos in `watch_repos`. API calls grow
with that list; raise `SCALE_INTERVAL` if you watch many.

## Building locally

```sh
git clone https://github.com/Datalab-Rotterdam/selfhosted-runner && cd selfhosted-runner
cp runners.example.yaml runners.yaml   # drop the `image:` line to use the local build
cp .env.example .env
make up        # build + start      make logs      make down   # remove runners + coordinator
```

Build args: `RUNNER_VERSION` (default 2.338.0), `RUNNER_SHA256_X64`, `RUNNER_SHA256_ARM64` (arm64 checksum
not verified if unset). Images are published by [publish.yml](.github/workflows/publish.yml) on every push to `main`.

## Releases and versioning

Versions are automated with [semantic-release](https://semantic-release.gitbook.io) from
[Conventional Commits](https://www.conventionalcommits.org) on `main`: `fix:` → patch, `feat:` → minor,
`feat!:` / a `BREAKING CHANGE:` footer → major. `docs:`, `ci:`, `chore:` etc. don't release. A release creates
a `vX.Y.Z` tag and GitHub release, and both images get the tags `X.Y.Z`, `X.Y`, `X`, `latest` and `sha-…`:

```sh
docker pull ghcr.io/datalab-rotterdam/selfhosted-runner:1        # follow a major
docker pull ghcr.io/datalab-rotterdam/selfhosted-runner:1.2.3    # pin exactly
```

## Security

- Don't attach self-hosted runners to public repos: fork PRs can run arbitrary code on your host.
- The coordinator mounts the host `docker.sock`, which is root-equivalent access to the host. Keep it private.
- `docker_socket: true` gives workflows the same access.
- PATs live in `.env`; keep it out of git (it's in `.gitignore` here, add it to your own folder too).
