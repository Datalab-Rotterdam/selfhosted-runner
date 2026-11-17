#!/usr/bin/env python3
"""Watch runners.yaml and reconcile runner containers via the docker CLI (host docker.sock).

Repos with `replicas: N` get N long-lived runners. Repos with `autoscale: {min, max}` get
ephemeral runners spawned on demand, based on queued jobs reported by the GitHub API.

Registration is verified with the same PAT the runners use (listing runners needs the same
permission as registering them). Permanent errors (bad PAT, no access) and repeated startup
failures block the repo instead of retrying forever.
"""
import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yaml

CONFIG = Path(os.environ.get("RUNNERS_CONFIG", "/config/runners.yaml"))
INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
SCALE_INTERVAL = int(os.environ.get("SCALE_INTERVAL", "15"))  # GitHub API polling
STOP_TIMEOUT = os.environ.get("STOP_TIMEOUT", "60")  # time for runner to deregister
REGISTER_TIMEOUT = int(os.environ.get("REGISTER_TIMEOUT", "120"))  # warn if not online by then
MAX_FAILURES = int(os.environ.get("MAX_FAILURES", "3"))  # startup failures before blocking
PULL_INTERVAL = int(os.environ.get("IMAGE_PULL_INTERVAL", "3600"))  # 0 = never pull, use local images
COORD_ID = os.environ.get("COORDINATOR_ID", "default")  # set a unique id per coordinator sharing one docker host
LABEL = "selfhosted-runner.managed"
COORD_LABEL = "selfhosted-runner.coordinator"
HASH_LABEL = "selfhosted-runner.hash"
POOL_LABEL = "selfhosted-runner.pool"
IMAGE_LABEL = "selfhosted-runner.images"  # image IDs the runner (and its daemon) were started from
SIDECAR_LABEL = "selfhosted-runner.sidecar"  # value: the runner the dind daemon belongs to
DIND_IMAGE = "docker:29-dind"
WORK_DIR = "/home/runner/actions-runner/_work"
TOKEN_FILE = ".registration-token"  # in /home/runner; entrypoint.sh deletes it after registering
RUNNER_UID = 1001
PROTOCOL_LABEL = "selfhosted-runner.protocol"
PROTOCOL = "2"  # runner image reads its registration token from TOKEN_FILE

HINTS = {
    401: "PAT rejected (invalid or expired)",
    403: "PAT lacks permission (repo: Administration read/write [+ Actions read for autoscale]; org: admin:org)",
    404: "not found: check the URL, and that the PAT has access to it",
}

started_at, registered, warned, next_try = {}, set(), set(), {}


class Fatal(Exception):
    """Permanent API error: retrying won't help."""


class Group:
    """One runners.yaml repo entry: shared PAT, API scope and health state."""

    def __init__(self, url, scope, pat, api):
        self.url, self.scope, self.pat, self.api = url, scope, pat, api
        self.online, self.busy = set(), set()
        self.last_refresh, self.checked, self.fails = 0.0, False, 0
        self.blocked = None
        self.next_spawn = 0.0

    def block(self, reason):
        if not self.blocked:
            self.blocked = reason
            log(f"{self.url}: BLOCKED, not retrying: {reason}. "
                "Fix it, then edit runners.yaml (URL/settings) or recreate the coordinator (new PAT).")

    def call(self, route, method="GET"):
        req = urllib.request.Request(f"{self.api}/{route}", method=method, headers={
            "Authorization": f"Bearer {self.pat}", "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            limited = e.headers.get("X-RateLimit-Remaining") == "0"
            if e.code in HINTS and not limited:
                try:
                    msg = json.load(e).get("message", "")
                except Exception:
                    msg = ""
                raise Fatal(f"{HINTS[e.code]} [{e.code} {route.split('?')[0]}: {msg}]")
            raise

    def refresh(self):
        """Poll runner list (doubles as PAT/URL preflight). True if fresh data was fetched."""
        if self.blocked or time.time() - self.last_refresh < SCALE_INTERVAL:
            return False
        self.last_refresh = time.time()
        try:
            runners = self.call(f"{self.scope}/actions/runners?per_page=100")["runners"]
        except Fatal as e:
            self.block(str(e))
            return False
        except Exception as e:
            log(f"{self.url}: GitHub API error (will retry): {e}")
            return False
        self.online = {r["name"] for r in runners if r["status"] == "online"}
        self.busy = {r["name"] for r in runners if r["busy"]}
        if not self.checked:
            log(f"{self.url}: PAT ok, {len(runners)} runner(s) registered")
        self.checked = True
        return True

    def registration_token(self):
        """Short-lived (1h) token that can only register runners: runners never see the PAT."""
        return self.call(f"{self.scope}/actions/runners/registration-token", method="POST")["token"]

    def deregister(self, name):
        """Remove a runner's registration (the container holds no PAT to do it itself)."""
        try:
            runners = self.call(f"{self.scope}/actions/runners?per_page=100")["runners"]
            for r in runners:
                if r["name"] == name:
                    self.call(f"{self.scope}/actions/runners/{r['id']}", method="DELETE")
                    log(f"{name}: deregistered")
        except Exception as e:  # ephemeral runners deregister themselves; GitHub prunes the rest
            log(f"{name}: could not deregister: {e}")

    @property
    def ready(self):
        return self.checked and not self.blocked


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def docker(*args, env=None, check=True, data=None):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, input=data, text=data is None,
        env={**os.environ, **(env or {})},
    )


def token_tar(token):
    """Tar stream for `docker cp -`: the token as a file only the runner user can read.

    Not an env var: a container's initial environment stays readable (/proc/1/environ, docker inspect)
    and jobs have sudo, so they could register rogue runners with it."""
    buf, data = io.BytesIO(), token.encode()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(TOKEN_FILE)
        info.size, info.mode, info.uid, info.gid = len(data), 0o400, RUNNER_UID, RUNNER_UID
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def spec_hash(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def parse(cfg):
    """-> (fixed: name -> (spec, group), pools: slug -> pool)"""
    defaults = cfg.get("defaults", {})
    image = cfg.get("image", "selfhosted-runner:local")
    prefix = cfg.get("name_prefix", "runner")
    fixed, pools, seen = {}, {}, set()
    for repo in cfg["repos"]:
        opts = {**defaults, **repo}
        url = opts["url"].rstrip("/")
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
        if slug in seen:
            raise ValueError(f"duplicate url: {url}")
        seen.add(slug)
        pat = os.environ.get(opts["pat_env"])
        if not pat:
            raise ValueError(f"env var {opts['pat_env']} not set (add to .env)")
        auto = opts.get("autoscale")
        is_org = "/" not in path
        api = opts.get("api_url", "https://api.github.com")
        group = Group(url, f"orgs/{path}" if is_org else f"repos/{path}", pat, api)
        env = {
            "REPO_URL": url,
            "RUNNER_LABELS": ",".join(opts.get("labels", [])),
            "RUNNER_EPHEMERAL": "true" if auto else str(bool(opts.get("ephemeral", False))).lower(),
        }
        if opts.get("group"):
            env["RUNNER_GROUP"] = opts["group"]
        if opts.get("api_url"):
            env["GITHUB_API_URL"] = opts["api_url"]
        mode = opts.get("docker") or ("socket" if opts.get("docker_socket") else None)
        if mode not in (None, "socket", "dind"):
            raise ValueError(f"docker must be `dind` or `socket`: {url}")
        base = {"image": image, "env": env, "docker": mode,
                "dind_image": opts.get("dind_image", DIND_IMAGE),
                "dind_runtime": opts.get("dind_runtime"),
                "dind_dns": opts.get("dind_dns"),
                "cpus": opts.get("cpus"), "memory": opts.get("memory"),
                "devices": list(opts.get("devices") or [])}

        if auto:
            if is_org and not opts.get("watch_repos"):
                raise ValueError(f"org autoscale needs `watch_repos` (no org-wide queue API): {url}")
            watch = [f"{path}/{r}" for r in opts["watch_repos"]] if is_org else [path]
            pools[slug] = {
                "base": base, "prefix": f"{prefix}-{slug}", "watch": watch, "group": group,
                "min": int(auto.get("min", 0)), "max": int(auto.get("max", 5)),
            }
            continue
        for i in range(1, int(opts.get("replicas", 1)) + 1):
            name = f"{prefix}-{slug}-{i}"
            fixed[name] = ({**base, "env": {**env, "RUNNER_NAME": name}}, group)
    return fixed, pools


def existing():
    """name -> {hash, pool, state, status}"""
    fmt = '{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Label "%s"}}\t{{.Label "%s"}}\t{{.Label "%s"}}' % (
        HASH_LABEL, POOL_LABEL, IMAGE_LABEL)
    res = docker("ps", "-a", "--filter", f"label={LABEL}=true",
                 "--filter", f"label={COORD_LABEL}={COORD_ID}", "--format", fmt)
    out = {}
    for line in res.stdout.splitlines():
        name, state, status, h, pool, images = line.split("\t")
        out[name] = {"hash": h, "pool": pool, "state": state, "status": status, "images": images}
    return out


class Images:
    """Pulls the configured images and pins each tag to an image ID.

    Containers start from the pinned ID, so a pull never changes a runner mid-flight, and a runner image
    is only adopted if it speaks this coordinator's PROTOCOL: a newer runner release than the coordinator
    keeps the last compatible image instead of breaking every runner."""

    def __init__(self):
        self.pinned, self.last_pull, self.warned = {}, {}, set()

    def image_id(self, ref):
        res = docker("image", "inspect", "--format", "{{.Id}}\t{{with .Config.Labels}}{{index . \"%s\"}}{{end}}" % PROTOCOL_LABEL,
                     ref, check=False)
        if res.returncode:
            return None, None
        image_id, _, proto = res.stdout.rstrip("\n").partition("\t")
        return image_id, proto

    def refresh(self, ref, runner):
        """Pull `ref` when due; return the pinned image ID (or the ref itself if nothing is local yet)."""
        due = PULL_INTERVAL > 0 and time.time() - self.last_pull.get(ref, 0) >= PULL_INTERVAL
        if due or ref not in self.pinned:
            if due:
                self.last_pull[ref] = time.time()
                res = docker("pull", "-q", ref, check=False)
                if res.returncode and ref not in self.warned:  # local-only tag, registry down...
                    self.warned.add(ref)
                    log(f"cannot pull {ref}, using the local image: {res.stderr.strip().splitlines()[-1:]}")
            new, proto = self.image_id(ref)
            old = self.pinned.get(ref)
            if old and self.image_id(old)[0] is None:  # removed behind our back
                del self.pinned[ref]
                old = None
            if new and new != old:
                if runner and proto != PROTOCOL and old:
                    if (ref, new) not in self.warned:
                        self.warned.add((ref, new))
                        log(f"{ref}: new image needs runner protocol {proto or 'none'}, this coordinator speaks "
                            f"{PROTOCOL}; keeping {old[:19]}. Update the coordinator image.")
                else:
                    if old:
                        log(f"{ref}: updated to {new[:19]}, replacing idle runners")
                    self.pinned[ref] = new
                    # own tag: an untagged image can be removed (pull moving the tag, image prune) while
                    # runners still need to be restarted from it
                    docker("tag", new, f"selfhosted-runner-pinned:{COORD_ID}-{hashlib.sha256(ref.encode()).hexdigest()[:12]}",
                           check=False)
        return self.pinned.get(ref, ref)

    def resolve(self, spec):
        """-> (runner image, dind image, label value naming both)"""
        runner = self.refresh(spec["image"], True)
        dind = self.refresh(spec["dind_image"], False) if spec["docker"] == "dind" else ""
        return runner, dind, f"{runner}+{dind}"


images = Images()


def check_coordinator_image():
    """Log when a newer image for this coordinator itself is available (it cannot replace itself safely)."""
    me = docker("inspect", "--format", "{{.Config.Image}}\t{{.Image}}", os.environ.get("HOSTNAME", ""), check=False)
    if me.returncode:
        return
    ref, _, current = me.stdout.rstrip("\n").partition("\t")
    if docker("pull", "-q", ref, check=False).returncode == 0:
        new, _ = images.image_id(ref)
        if new and new != current and ("coordinator", new) not in images.warned:
            images.warned.add(("coordinator", new))
            log(f"newer coordinator image {ref} available: `docker compose pull && docker compose up -d`")


def sidecars():
    """runner name -> dind container name, for this coordinator"""
    res = docker("ps", "-a", "--filter", f"label={SIDECAR_LABEL}", "--filter", f"label={COORD_LABEL}={COORD_ID}",
                 "--format", '{{.Names}}\t{{.Label "%s"}}' % SIDECAR_LABEL)
    return {owner: name for name, owner in (l.split("\t") for l in res.stdout.splitlines())}


def drop_sidecar(name):
    """Remove a runner's dind daemon, network and volumes (fresh daemon per runner)."""
    docker("rm", "-f", "-v", f"{name}-dind", check=False)
    docker("network", "rm", f"{name}-net", check=False)
    docker("volume", "rm", "-f", f"{name}-certs", f"{name}-work", check=False)


def limits(spec):
    """Resource limits and host devices, applied to the runner and its daemon alike."""
    args = [a for d in spec.get("devices", []) for a in ("--device", d)]
    if spec.get("cpus"):
        args += ["--cpus", str(spec["cpus"])]
    if spec.get("memory"):
        args += ["--memory", str(spec["memory"])]
    return args


_host_dns = None


def host_dns(image):
    """Upstream DNS servers as the host daemon hands them to containers on the default bridge.

    A daemon on a user network only sees Docker's embedded DNS (127.0.0.11), which its own containers
    cannot reach, so it would fall back to 8.8.8.8/8.8.4.4: blocked on many networks."""
    global _host_dns
    if _host_dns is None:
        res = docker("run", "--rm", "--network", "bridge", "--entrypoint", "cat", image, "/etc/resolv.conf",
                     check=False)
        _host_dns = [l.split()[1] for l in res.stdout.splitlines()
                     if l.startswith("nameserver") and not l.split()[1].startswith("127.")]
        log(f"dind upstream DNS: {', '.join(_host_dns) or 'none found, daemon default'}")
    return _host_dns


def start_sidecar(name, spec, dind_image):
    """Private Docker daemon for one runner: own network, TLS client certs, shared workspace.

    Jobs get no access to the host daemon (other runners, the coordinator and its PATs). The daemon
    runs --privileged unless `dind_runtime` (e.g. sysbox-runc) can run it unprivileged."""
    drop_sidecar(name)
    labels = ["--label", f"{SIDECAR_LABEL}={name}", "--label", f"{COORD_LABEL}={COORD_ID}"]
    docker("network", "create", *labels, f"{name}-net")
    args = ["run", "-d", "--name", f"{name}-dind", *labels, "--network", f"{name}-net",
            "--network-alias", "docker", "-e", "DOCKER_TLS_CERTDIR=/certs",
            "-v", f"{name}-certs:/certs", "-v", f"{name}-work:{WORK_DIR}", *limits(spec)]
    if spec.get("dind_runtime"):
        args += ["--runtime", spec["dind_runtime"]]
    else:
        args += ["--privileged"]
    dns = spec.get("dind_dns") or host_dns(spec["dind_image"])
    docker(*args, dind_image, *(a for d in dns for a in ("--dns", d)))


def forget(name):
    for s in (registered, warned):
        s.discard(name)
    started_at.pop(name, None)


def remove(name, group=None):
    log(f"removing {name}")
    docker("stop", "-t", STOP_TIMEOUT, name, check=False)
    docker("rm", "-f", name, check=False)
    drop_sidecar(name)
    if group:
        group.deregister(name)
    forget(name)


def start(name, spec, group, pool=""):
    log(f"starting {name}")
    forget(name)
    try:
        token = group.registration_token()
    except Fatal as e:
        group.block(f"cannot get a registration token: {e}")
        return False
    except Exception as e:
        log(f"{name}: cannot get a registration token (will retry): {e}")
        next_try[name] = group.next_spawn = time.time() + 30
        return False
    env = {**spec["env"], "RUNNER_NAME": name}
    runner_image, dind_image, image_label = images.resolve(spec)
    # no docker restart policy: the coordinator restarts runners so it can tell crashes from startup failures
    args = ["create", "--name", name,
            "--label", f"{LABEL}=true", "--label", f"{HASH_LABEL}={spec_hash(spec)}",
            "--label", f"{POOL_LABEL}={pool}", "--label", f"{COORD_LABEL}={COORD_ID}",
            "--label", f"{IMAGE_LABEL}={image_label}", *limits(spec)]
    if spec["docker"] == "dind":
        # in argv, not the subprocess env: there they would redirect the coordinator's own docker CLI
        args += ["-e", "DOCKER_HOST=tcp://docker:2376", "-e", "DOCKER_TLS_VERIFY=1",
                 "-e", "DOCKER_CERT_PATH=/certs/client"]
        args += ["--network", f"{name}-net", "-v", f"{name}-certs:/certs:ro", "-v", f"{name}-work:{WORK_DIR}"]
    elif spec["docker"] == "socket":
        args += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
    for k in env:
        args += ["-e", k]  # value passed via subprocess env, not argv
    try:
        if spec["docker"] == "dind":
            start_sidecar(name, spec, dind_image)
        docker(*args, runner_image, env=env)
        proto = docker("inspect", "--format", '{{with .Config.Labels}}{{index . "%s"}}{{end}}' % PROTOCOL_LABEL, name).stdout.strip()
        if proto != PROTOCOL:
            docker("rm", "-f", name, check=False)
            drop_sidecar(name)
            group.block(f"runner image {spec['image']} is older than this coordinator (protocol {proto or 'none'}, "
                        f"need {PROTOCOL}): use the same release for both images, or `make build` for a local one")
            return False
        docker("cp", "-", f"{name}:/home/runner/", data=token_tar(token))
        docker("start", name)
        res = None
    except subprocess.CalledProcessError as e:
        res = e
    if res:
        docker("rm", "-f", name, check=False)
        drop_sidecar(name)
        err = res.stderr.decode() if isinstance(res.stderr, bytes) else res.stderr
        reason = err.strip().replace("\n", " | ")
        group.fails += 1
        log(f"failed to start {name} [{group.fails}/{MAX_FAILURES}]: {reason}")
        group.next_spawn = next_try[name] = time.time() + 10 * 2 ** group.fails
        if group.fails >= MAX_FAILURES:
            group.block(f"could not start {MAX_FAILURES} runners in a row, last error: {reason}")
        return False
    started_at[name] = time.time()
    return True


def exit_code(info):
    m = re.search(r"Exited \((\d+)\)", info["status"])
    return int(m.group(1)) if m else 1


def handle_exit(name, info, group):
    """Container stopped: remove it, and count it as a startup failure if it never registered."""
    code = exit_code(info)
    if name in registered or code == 0:
        log(f"{name}: exited (code {code})")
    else:
        tail = docker("logs", "--tail", "5", name, check=False)
        reason = (tail.stdout + tail.stderr).strip().replace("\n", " | ")
        group.fails += 1
        log(f"{name}: exited (code {code}) before registering "
            f"[{group.fails}/{MAX_FAILURES}]: {reason}")
        group.next_spawn = time.time() + 10 * 2 ** group.fails
        next_try[name] = group.next_spawn
        if group.fails >= MAX_FAILURES:
            group.block(f"{MAX_FAILURES} runner startups failed in a row, last error: {reason}")
    docker("rm", "-f", name, check=False)
    drop_sidecar(name)
    forget(name)


def check_registration(name, group):
    if name in group.online and name not in registered:
        registered.add(name)
        group.fails = 0
        log(f"{name}: registered and online")
    elif (name not in registered and name not in warned
          and time.time() - started_at.get(name, time.time()) > REGISTER_TIMEOUT):
        warned.add(name)
        log(f"{name}: not online after {REGISTER_TIMEOUT}s, check `docker logs {name}`")


def queued_jobs(pool):
    """Queued jobs that target self-hosted runners."""
    group, n = pool["group"], 0
    for repo in pool["watch"]:
        for status in ("queued", "in_progress"):
            runs = group.call(f"repos/{repo}/actions/runs?status={status}&per_page=100")["workflow_runs"]
            for run in runs:
                jobs = group.call(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
                n += sum(1 for j in jobs if j["status"] == "queued" and "self-hosted" in j["labels"])
    return n


def scale_pool(slug, pool, alive):
    group = pool["group"]
    try:
        queued = queued_jobs(pool)
    except Fatal as e:
        group.block(f"cannot read workflow runs: {e}")
        return
    except Exception as e:
        log(f"{slug}: GitHub API error (will retry): {e}")
        return
    busy = group.busy & alive
    target = max(pool["min"], min(pool["max"], len(busy) + queued))
    if len(alive) < target and time.time() >= group.next_spawn:
        log(f"{slug}: queued={queued} busy={len(busy)} alive={len(alive)} -> +{target - len(alive)}")
        for _ in range(target - len(alive)):
            if not start(f"{pool['prefix']}-{uuid.uuid4().hex[:6]}", pool["base"], group, pool=slug):
                break
    elif len(alive) > target:
        for name in [n for n in alive if n not in busy][: len(alive) - target]:
            remove(name, group)


def idle(name, group):
    """Fresh check (not the cached list): replacing a runner that just took a job would kill the job."""
    try:
        runners = group.call(f"{group.scope}/actions/runners?per_page=100")["runners"]
    except Exception:
        return False
    return any(r["name"] == name and not r["busy"] for r in runners)


def reconcile(fixed, pools):
    groups = {id(g): g for _, g in fixed.values()} | {id(p["group"]): p["group"] for p in pools.values()}
    refreshed = {id(g): g.refresh() for g in groups.values()}
    current = {}  # spec hash -> image label runners should carry

    alive = {slug: set() for slug in pools}
    have = existing()
    for owner in sidecars().keys() - have.keys():  # runner gone (e.g. coordinator restarted mid-removal)
        drop_sidecar(owner)
    for name, info in have.items():
        pool = pools.get(info["pool"]) if info["pool"] else None
        if pool:
            spec, group = pool["base"], pool["group"]
        elif name in fixed:
            (spec, group) = fixed[name]
        else:
            remove(name)  # no longer in config
            continue
        if info["hash"] != spec_hash(spec):
            remove(name, group)
        elif info["state"] != "running":
            handle_exit(name, info, group)
        elif (info["images"] != current.setdefault(info["hash"], images.resolve(spec)[2])
              and name in registered and name not in group.busy and idle(name, group)):
            remove(name, group)  # image updated; restarted below from the new one
            continue
        else:
            check_registration(name, group)
            if pool:
                alive[info["pool"]].add(name)

    have = existing()
    for name, (spec, group) in fixed.items():
        if name not in have and group.ready and time.time() >= next_try.get(name, 0):
            start(name, spec, group)
    for slug, pool in pools.items():
        if pool["group"].ready and refreshed[id(pool["group"])]:
            scale_pool(slug, pool, alive[slug])


def down():
    try:
        fixed, pools = parse(yaml.safe_load(CONFIG.read_bytes()))
    except Exception as e:
        log(f"cannot read {CONFIG}, removing runners without deregistering: {e}")
        fixed, pools = {}, {}
    for name, info in existing().items():
        pool = pools.get(info["pool"])
        group = pool["group"] if pool else fixed.get(name, (None, None))[1]
        remove(name, group)
    nets = docker("network", "ls", "--filter", f"label={SIDECAR_LABEL}", "--filter",
                  f"label={COORD_LABEL}={COORD_ID}", "--format", '{{.Label "%s"}}' % SIDECAR_LABEL)
    for owner in set(sidecars()) | set(nets.stdout.split()):
        drop_sidecar(owner)


def main():
    if "--down" in sys.argv:
        down()
        return
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # leave runners running
    fixed, pools, last = {}, {}, None
    coord_checked = 0.0
    while True:
        raw = None
        try:
            if PULL_INTERVAL > 0 and time.time() - coord_checked >= PULL_INTERVAL:
                coord_checked = time.time()
                check_coordinator_image()
            raw = CONFIG.read_bytes()
            if raw != last:
                log(f"loading {CONFIG}")
                fixed, pools = parse(yaml.safe_load(raw))
                last = raw
            reconcile(fixed, pools)
        except Exception as e:  # keep last good config on bad edits
            log(f"error: {e}")
            last = None if raw is None else raw
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
