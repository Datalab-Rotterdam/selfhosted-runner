#!/usr/bin/env python3
"""Watch runners.yaml and reconcile runner containers via the docker CLI (host docker.sock).

Repos with `replicas: N` get N long-lived runners. Repos with `autoscale: {min, max}` get
ephemeral runners spawned on demand, based on queued jobs reported by the GitHub API.

Registration is verified with the same PAT the runners use (listing runners needs the same
permission as registering them). Permanent errors (bad PAT, no access) and repeated startup
failures block the repo instead of retrying forever.
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
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
COORD_ID = os.environ.get("COORDINATOR_ID", "default")  # set a unique id per coordinator sharing one docker host
LABEL = "selfhosted-runner.managed"
COORD_LABEL = "selfhosted-runner.coordinator"
HASH_LABEL = "selfhosted-runner.hash"
POOL_LABEL = "selfhosted-runner.pool"

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

    def call(self, route):
        req = urllib.request.Request(f"{self.api}/{route}", headers={
            "Authorization": f"Bearer {self.pat}", "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
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

    @property
    def ready(self):
        return self.checked and not self.blocked


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def docker(*args, env=None, check=True):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )


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
            "GITHUB_PAT": pat,
            "RUNNER_LABELS": ",".join(opts.get("labels", [])),
            "RUNNER_EPHEMERAL": "true" if auto else str(bool(opts.get("ephemeral", False))).lower(),
        }
        if opts.get("group"):
            env["RUNNER_GROUP"] = opts["group"]
        if opts.get("api_url"):
            env["GITHUB_API_URL"] = opts["api_url"]
        base = {"image": image, "env": env, "docker_socket": bool(opts.get("docker_socket"))}

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
    fmt = '{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Label "%s"}}\t{{.Label "%s"}}' % (HASH_LABEL, POOL_LABEL)
    res = docker("ps", "-a", "--filter", f"label={LABEL}=true",
                 "--filter", f"label={COORD_LABEL}={COORD_ID}", "--format", fmt)
    out = {}
    for line in res.stdout.splitlines():
        name, state, status, h, pool = line.split("\t")
        out[name] = {"hash": h, "pool": pool, "state": state, "status": status}
    return out


def forget(name):
    for s in (registered, warned):
        s.discard(name)
    started_at.pop(name, None)


def remove(name):
    log(f"removing {name}")
    docker("stop", "-t", STOP_TIMEOUT, name, check=False)
    docker("rm", "-f", name, check=False)
    forget(name)


def start(name, spec, pool=""):
    log(f"starting {name}")
    forget(name)
    env = {**spec["env"], "RUNNER_NAME": name}
    # no docker restart policy: the coordinator restarts runners so it can tell crashes from startup failures
    args = ["run", "-d", "--name", name,
            "--label", f"{LABEL}=true", "--label", f"{HASH_LABEL}={spec_hash(spec)}",
            "--label", f"{POOL_LABEL}={pool}", "--label", f"{COORD_LABEL}={COORD_ID}"]
    for k in env:
        args += ["-e", k]  # value passed via subprocess env, not argv
    if spec["docker_socket"]:
        args += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
    res = docker(*args, spec["image"], env=env, check=False)
    if res.returncode:
        log(f"failed to start {name}: {res.stderr.strip()}")
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
            start(f"{pool['prefix']}-{uuid.uuid4().hex[:6]}", pool["base"], pool=slug)
    elif len(alive) > target:
        for name in [n for n in alive if n not in busy][: len(alive) - target]:
            remove(name)


def reconcile(fixed, pools):
    groups = {id(g): g for _, g in fixed.values()} | {id(p["group"]): p["group"] for p in pools.values()}
    refreshed = {id(g): g.refresh() for g in groups.values()}

    alive = {slug: set() for slug in pools}
    for name, info in existing().items():
        pool = pools.get(info["pool"]) if info["pool"] else None
        if pool:
            spec, group = pool["base"], pool["group"]
        elif name in fixed:
            (spec, group) = fixed[name]
        else:
            remove(name)  # no longer in config
            continue
        if info["hash"] != spec_hash(spec):
            remove(name)
        elif info["state"] != "running":
            handle_exit(name, info, group)
        else:
            check_registration(name, group)
            if pool:
                alive[info["pool"]].add(name)

    have = existing()
    for name, (spec, group) in fixed.items():
        if name not in have and group.ready and time.time() >= next_try.get(name, 0):
            start(name, spec)
    for slug, pool in pools.items():
        if pool["group"].ready and refreshed[id(pool["group"])]:
            scale_pool(slug, pool, alive[slug])


def down():
    for name in existing():
        remove(name)


def main():
    if "--down" in sys.argv:
        down()
        return
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # leave runners running
    fixed, pools, last = {}, {}, None
    while True:
        raw = None
        try:
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
