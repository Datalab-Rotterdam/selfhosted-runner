# Examples

Each folder is a complete, self-contained setup that pulls the prebuilt images from GHCR (no clone or
build needed). Copy a folder, edit `runners.yaml`, add your PAT(s), and start it.

| folder | shows |
|---|---|
| [single-repo](single-repo) | one repo, 2 fixed runners |
| [multi-repo](multi-repo) | several repos, shared `defaults`, per-repo labels, one autoscaled repo |
| [org-autoscale](org-autoscale) | org-level runners scaled by queued jobs in watched repos |

## Creating the PAT

The coordinator and every runner use the PAT to request registration tokens, list runners and (for autoscale)
read queued jobs. Create one per example (or one shared by several repos) and put it in `.env`.
Prefer a **fine-grained** token scoped to only the repos/org you use.

### What each example needs

Fine-grained PAT, set these in the token's permission list (anything not listed stays *No access*):

| Permission | Level | single-repo | multi-repo | org-autoscale |
|---|---|:-:|:-:|:-:|
| Repository → **Metadata** | Read-only | auto | auto | auto |
| Repository → **Administration** | **Read and write** | yes | yes | no |
| Repository → **Actions** | **Read-only** | no | yes (autoscaled repo only) | yes (each `watch_repos` repo) |
| Organization → **Self-hosted runners** | **Read and write** | no | no | yes |

| Setting | single-repo | multi-repo | org-autoscale |
|---|---|---|---|
| **Resource owner** | owner of the repo | owner of the repos | the org |
| **Repository access** | the repo | all repos in `runners.yaml` | the `watch_repos` repos |

Classic PAT scopes instead: single-repo `repo`, multi-repo `repo`, org-autoscale `admin:org` + `repo`.

These come from GitHub's [permissions required for fine-grained PATs](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
(registration/remove token: Administration or Self-hosted runners *write*; listing runners: *read*; workflow runs and jobs: Actions *read*).

Fixed `replicas` runners never need Actions: Read; only `autoscale` does.
No other permission is needed for registration checks: the coordinator lists runners with the same permission
it uses to register them.

### Fine-grained token (recommended)

1. GitHub → your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Resource owner**: the user or the org that owns the repos. For org runners it must be the org.
   (Org owners may need to allow fine-grained tokens: org **Settings → Personal access tokens**, and approve the
   token request if approval is required.)
3. **Expiration**: pick a date and note it; an expired token shows up as `BLOCKED … PAT rejected` in the coordinator logs.
4. **Repository access**: *Only select repositories*, and choose the repos from `runners.yaml`
   (for org runners with autoscale: the `watch_repos`).
5. **Permissions** (see the table above):
   - Repository permissions → **Administration**: Read and write (repo runners)
   - Repository permissions → **Actions**: Read (autoscale only)
   - Organization permissions → **Self-hosted runners**: Read and write (org runners)
6. **Generate token** and copy it right away (it is shown once).

### Classic token

**Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token (classic)**, then tick
`repo` for repo runners, or `admin:org` (plus `repo` if autoscale watches private repos) for org runners.
For orgs that enforce SSO, click **Configure SSO → Authorize** next to the token.

### Put it in `.env`

```sh
cp .env.example .env
# edit .env, one line per pat_env name used in runners.yaml:
GH_PAT=github_pat_xxxxxxxx
```

`.env` is read when the coordinator container is created, so after changing a PAT run
`docker compose up -d --force-recreate coordinator`. Never commit `.env` (the examples ignore it).

### Check that it works

```sh
docker compose logs -f coordinator
```

| log line | meaning |
|---|---|
| `PAT ok, N runner(s) registered` | token and URL are fine |
| `registered and online` | a runner is connected to GitHub |
| `BLOCKED … PAT rejected (invalid or expired)` | wrong, revoked or expired token |
| `BLOCKED … PAT lacks permission` | missing a permission from the table above (or org approval/SSO) |
| `BLOCKED … not found` | wrong URL, or the token can't see that repo/org |

You can also test a token by hand (should return JSON with `total_count`, not an error):

```sh
curl -s -H "Authorization: Bearer $GH_PAT" https://api.github.com/repos/YOUR-ORG/YOUR-REPO/actions/runners
```

## Run an example

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

The full option list is in the [root README](../README.md).
