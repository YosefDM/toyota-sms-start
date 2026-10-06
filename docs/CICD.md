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

### 2. Deploy to the rig over IAP — keyless via Workload Identity Federation

The deploy runs on a GitHub-hosted runner that authenticates to GCP **keylessly** (the runner's GitHub
OIDC token is exchanged for short-lived SA credentials via Workload Identity Federation) and reaches the
VM through an **IAP tunnel**. No service-account key is created or stored — this project's org policy
(`constraints/iam.disableServiceAccountKeyCreation`) blocks SA keys anyway, and WIF is the better path.

> **This is already set up for this repo.** The steps below record what was done, for rebuild/audit.

**a. Service account + roles** (no OS Login — it would break the existing `Yosef` metadata-SSH login):

```bash
PROJECT=<project-id>;  PNUM=<project-number>;  REPO=YosefDM/toyota-sms-start;  ZONE=northamerica-northeast1-a
SA="toyota-deployer@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts create toyota-deployer --project "$PROJECT" --display-name "Toyota rig CI deployer"
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/iap.tunnelResourceAccessor --condition=None            # use the IAP tunnel
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/compute.instanceAdmin.v1 --condition=None              # describe the VM (instances.get)
# Enable the APIs WIF + IAP need:
gcloud services enable iamcredentials.googleapis.com iap.googleapis.com --project "$PROJECT"
```

Port 22 is already reachable from IAP's range `35.235.240.0/20` via the existing `default-allow-ssh`
rule, so no firewall change is needed.

**a2. Pre-provision a dedicated deploy SSH key.** The SA can mint credentials but isn't allowed to write
SSH keys into metadata on the fly, so we add the key once (as a project owner). This also keeps the key
off OS Login and leaves the `Yosef` project key untouched. The guest agent gives the `toyota-deployer`
user passwordless sudo automatically.

```bash
ssh-keygen -t ed25519 -f deploy_key -N "" -C toyota-deployer
# instance ssh-keys metadata starts empty here; if yours isn't, append rather than overwrite:
printf 'toyota-deployer:%s\n' "$(cat deploy_key.pub)" > ssh-keys.txt
gcloud compute instances add-metadata toyota-rig --project "$PROJECT" --zone "$ZONE" \
  --metadata-from-file ssh-keys=ssh-keys.txt
gh secret set SSH_PRIVATE_KEY < deploy_key
rm -f deploy_key deploy_key.pub ssh-keys.txt
```

**b. Workload Identity Federation**, scoped to this one repo:

```bash
gcloud iam workload-identity-pools create github-pool --project "$PROJECT" --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc github-provider --project "$PROJECT" \
  --location global --workload-identity-pool github-pool --display-name "GitHub OIDC" \
  --issuer-uri "https://token.actions.githubusercontent.com" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition "assertion.repository=='${REPO}'"
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PNUM}/locations/global/workloadIdentityPools/github-pool/attribute.repository/${REPO}"
```

**c. Repo secrets** (no key — just identifiers):

```bash
gh secret set WIF_PROVIDER --body "projects/${PNUM}/locations/global/workloadIdentityPools/github-pool/providers/github-provider"
gh secret set GCP_SA_EMAIL --body "$SA"
gh secret set GCP_PROJECT  --body "$PROJECT"
gh secret set GCP_ZONE     --body "$ZONE"
gh secret set GCP_VM       --body "toyota-rig"
gh secret set APP_USER     --body "Yosef"     # the VM user that owns the emulator/adb; the service runs as it
# SSH_PRIVATE_KEY was set in step a2.
```

**d. VM prerequisites** (once): `git`, `python3-venv`, and `curl` installed. The `toyota-deployer`
metadata-SSH user gets passwordless sudo automatically via the Google guest agent (`google-sudoers`).

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
