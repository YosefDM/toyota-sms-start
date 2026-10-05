# CI/CD

Three GitHub Actions workflows (`.github/workflows/`):

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | every PR + push to `master` | byte-compiles the server + runs the offline unit tests (`server/tests/`) |
| `claude-review.yml` | every PR | runs the `/code-review` skill and posts findings as PR comments |
| `deploy.yml` | merge to `master` touching `server/**` (+ manual) | deploys the webhook to the prod rig over an IAP tunnel |

Nothing sensitive is committed — the GCP project, zone, instance, VM username, and service-account
key all live in GitHub secrets. Until the secrets exist the deploy job **no-ops with a notice**
instead of failing, so it's safe to merge before finishing setup.

---

## One-time setup

### 1. Claude PR review — `CLAUDE_CODE_OAUTH_TOKEN`

1. Install the **Claude GitHub App** on the repo: <https://github.com/apps/claude> (needs repo admin).
2. Generate a subscription token locally and add it as a repo secret:
   ```bash
   claude setup-token           # prints a long-lived OAuth token tied to your Claude plan
   gh secret set CLAUDE_CODE_OAUTH_TOKEN   # paste the token
   ```
That's all the review workflow needs. (On a public repo, GitHub withholds secrets from *fork* PRs, so
reviews run only on PRs pushed to a branch in this repo — fine for a solo repo.)

### 2. Deploy to the rig over IAP — the `GCP_*` + `APP_USER` secrets

The deploy runs on a GitHub-hosted runner that authenticates to GCP with a **service-account key** and
reaches the VM through an **IAP tunnel** (the VM's port 22 is not open to the public internet).

**a. Create a deploy service account and grant it the three roles it needs.** Replace the placeholders
(`$PROJECT`, and keep the generated key file out of the repo):

```bash
PROJECT=your-gcp-project-id
gcloud iam service-accounts create toyota-deployer \
  --project "$PROJECT" --display-name "Toyota rig CI deployer"

SA="toyota-deployer@${PROJECT}.iam.gserviceaccount.com"

# use the IAP tunnel …
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:$SA" --role roles/iap.tunnelResourceAccessor
# … SSH in via OS Login WITH sudo (needed: the deploy writes a systemd unit) …
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:$SA" --role roles/compute.osAdminLogin
# … and look up the instance.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:$SA" --role roles/compute.viewer
```

**b. Make sure OS Login + the IAP SSH firewall are in place** (they already are if you SSH to the rig
via IAP today):

```bash
# OS Login on the instance (or set it project-wide):
gcloud compute instances add-metadata toyota-rig \
  --project "$PROJECT" --zone northamerica-northeast1-a \
  --metadata enable-oslogin=TRUE
# Firewall: allow IAP's range to reach port 22 (idempotent to re-run):
gcloud compute firewall-rules create allow-iap-ssh \
  --project "$PROJECT" --direction INGRESS --action ALLOW \
  --rules tcp:22 --source-ranges 35.235.240.0/20 || true
```

**c. Create a key and load all deploy secrets into the repo:**

```bash
gcloud iam service-accounts keys create sa-key.json --iam-account "$SA"

gh secret set GCP_SA_KEY  < sa-key.json
gh secret set GCP_PROJECT --body "$PROJECT"
gh secret set GCP_ZONE    --body "northamerica-northeast1-a"
gh secret set GCP_VM      --body "toyota-rig"
gh secret set APP_USER    --body "<the VM user that owns the emulator/adb>"   # the rig's login user

rm -f sa-key.json         # the key now lives only in the GitHub secret
```

`APP_USER` is the Linux user on the VM that launched the emulator (so the service shares that user's
adb server). The webhook service runs as this user.

**d. VM prerequisites** (once): `git`, `python3-venv`, and `curl` installed, and the OS-Login deploy
identity able to `sudo` without a password (granted by `roles/compute.osAdminLogin`).

---

## How a deploy works

On merge to `master` (or a manual **Run workflow**), `deploy.yml`:

1. authenticates to GCP with `GCP_SA_KEY`,
2. copies `scripts/deploy/remote_deploy.sh` to the VM over the IAP tunnel, and
3. runs it there. The script clones/fast-forwards the repo to the merged commit under
   `/opt/toyota-sms-remote` (owned by `APP_USER`), rebuilds the venv, (re)writes the
   `toyota-sms.service` systemd unit, and **restarts only that service.**

It never touches the emulator, `frida-server`, the `toyota-frida` bypass service, or the logged-in
Toyota session — a code push can't disturb the live rig's app state.

### The service's own config (`.env`) is NOT deployed

Twilio token, allowed numbers, and adb wiring live in `/opt/toyota-sms-remote/server/.env` on the VM
(gitignored, managed by hand — copy `server/.env.example`). The deploy leaves it alone. The unit
references it optionally, so the service also starts fine before a real `.env` exists (handy for a
first smoke-test: `/health` answers 200 even with no emulator attached).

### Exposing the webhook

uvicorn binds `127.0.0.1:8080`. Twilio needs a public HTTPS URL → run a tunnel on the VM (e.g.
Cloudflare Tunnel) pointing at `127.0.0.1:8080`, and set that URL as both Twilio's webhook and
`PUBLIC_URL` in `.env` (used for request-signature validation).
