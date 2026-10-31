# Examples

Each folder is a complete, self-contained setup that pulls the prebuilt images from GHCR (no clone or
build needed). Copy a folder, edit `runners.yaml`, add your PAT(s), and start it.

| folder | shows |
|---|---|
| [single-repo](single-repo) | one repo, 2 fixed runners |
| [multi-repo](multi-repo) | several repos, shared `defaults`, per-repo labels, one autoscaled repo |
| [org-autoscale](org-autoscale) | org-level runners scaled by queued jobs in watched repos |

```sh
cp -r examples/single-repo ~/my-runners && cd ~/my-runners
cp .env.example .env            # put your PAT(s) in .env
$EDITOR runners.yaml            # replace YOUR-ORG / YOUR-REPO
docker compose up -d
docker compose logs -f coordinator
```

Then check **Settings → Actions → Runners** on the repo (or org). Runners appear within ~20 seconds.
Use them with `runs-on: self-hosted` (or your labels).

Edit `runners.yaml` while it runs; the coordinator applies changes within seconds.
To stop and deregister everything:

```sh
docker compose run --rm coordinator --down
docker compose down
```

PAT scopes and the full option list are in the [root README](../README.md).
