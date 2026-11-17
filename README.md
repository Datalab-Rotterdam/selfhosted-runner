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
docker compose stop coordinator && docker compose run --rm coordinator --down && docker compose down
```
(`docker compose stop` alone stops only the coordinator; runners keep running. Stop it before `--down`,
or it restarts the runners that `--down` removes.)

If the GHCR packages are private, `docker login ghcr.io` first or make them public.

Run the coordinator and runner images from the same release. A coordinator newer than its runner image
blocks the entry with `runner image ... is older than this coordinator` instead of starting runners that
cannot register.

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
| `docker` | repo/defaults | `dind`: each runner gets its own Docker daemon (see [Docker in jobs](#docker-in-jobs)); `socket`: mount the host socket (unsafe) |
| `dind_image` | repo/defaults | daemon image for `docker: dind` (default `docker:29-dind`) |
| `dind_runtime` | repo/defaults | container runtime for the daemon, e.g. `sysbox-runc`; without it the daemon runs `--privileged` |
| `dind_dns` | repo/defaults | DNS servers for containers inside the daemon (default: the servers the host gives containers) |
| `cpus`, `memory` | repo/defaults | `docker run --cpus/--memory` for the runner and, with `dind`, its daemon |
| `devices` | repo/defaults | host devices for the runner and, with `dind`, its daemon, e.g. `[/dev/kvm]` (see [KVM](#kvm)) |
| `docker_socket` | repo/defaults | old spelling of `docker: socket` |
| `api_url` | repo/defaults | GitHub Enterprise API base URL |

Coordinator env vars: `IMAGE_PULL_INTERVAL` (default 3600s, see [Image updates](#image-updates)), `POLL_INTERVAL` (default 5s), `SCALE_INTERVAL` (GitHub API poll for autoscale, default 15s),
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

## Docker in jobs

The runner image ships the Docker CLI (with buildx and compose) but no daemon, plus the build basics GitHub-hosted
images have and workflows assume: `build-essential` (cgo), `pkg-config`, `python3`, `xz-utils` (`actions/setup-*`
archives), `zstd` and `file`. With `docker: dind`
the coordinator starts, next to each runner:

- a `docker:dind` daemon (`<runner>-dind`) on a network of its own (`<runner>-net`), reachable from the
  runner only, over TLS with client certs from a per-runner volume (`DOCKER_HOST=tcp://docker:2376`);
- a workspace volume mounted at the same path in runner and daemon, so
  `docker run -v "$GITHUB_WORKSPACE:/src" ...` works as on GitHub-hosted runners.

Daemon, network and volumes are removed with the runner. Combine it with `ephemeral: true` (or
`autoscale`) so every job starts with an empty daemon: no images, containers or files left by earlier jobs.

```yaml
defaults:
  ephemeral: true
  docker: dind
  cpus: 4
  memory: 16g
```

What this isolates, and what it does not:

- Jobs cannot reach the host daemon, so they cannot see or control other runners or the coordinator
  (whose environment holds every PAT). With `docker: socket` they can, and that is root on the host.
- Without `dind_runtime` the daemon runs `--privileged`, so jobs can run privileged containers
  (needed for cgroup, mount and loop-device tests). A job that sets out to can escape a privileged
  container onto the Docker host. Treat the host as belonging to the repos it serves, and only attach
  private repos you trust. For a hard boundary install [Sysbox](https://github.com/nestybox/sysbox) and
  set `dind_runtime: sysbox-runc`, or run the coordinator inside a dedicated VM.

## KVM

For jobs that boot VMs with QEMU/KVM, give the runners the host's `/dev/kvm`:

```yaml
defaults:
  devices: [/dev/kvm]
```

The runner user can use it directly, and with `docker: dind` so can containers that jobs start with
`--device /dev/kvm` or `--privileged`. The host needs hardware virtualization: on a VM (Proxmox,
Hyper-V, ...) turn on nested virtualization for it (Proxmox: CPU type `host`), and check that
`/dev/kvm` exists on the Docker host. A missing device makes runner startup fail and blocks the entry.

## Image updates

The coordinator pulls `image` and `dind_image` at startup and every `IMAGE_PULL_INTERVAL` seconds
(default 3600, `0` = never pull). When a tag points at a new image, idle runners are replaced; busy runners
finish their job first. Tags that only exist locally (`make build`) are used as they are.

A runner image is only adopted if it matches the coordinator's runner protocol; a newer, incompatible
runner release is skipped (logged) until the coordinator is updated. The coordinator can't replace its own
container: it logs when a newer coordinator image is available, update it with
`docker compose pull && docker compose up -d`. Pin both images to a major tag (`:1`) to take fixes and
features automatically but not breaking changes.

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
- `docker: socket` gives workflows the same access. Prefer `docker: dind` (see [Docker in jobs](#docker-in-jobs)).
- Runners never get a PAT: the coordinator mints a one-hour registration token per runner, copies it
  in as a file the entrypoint deletes after registering (never an env var, which stays readable in
  `/proc/1/environ` and `docker inspect`), and deregisters runners through the API. Jobs inherit the runner's environment and have `sudo`, so anything
  in the container is readable by jobs. (Running `entrypoint.sh` standalone with `GITHUB_PAT` still works,
  but then the PAT is in the container.)
- A runner removed because its repo entry was deleted from `runners.yaml` cannot be deregistered (its
  PAT may be gone); GitHub prunes offline self-hosted runners after 14 days.
- PATs live in `.env`; keep it out of git (it's in `.gitignore` here, add it to your own folder too).
