# Go-live checklist

Everything the `mickzijdel/aisafety-radar` repository needs before the first real daily run,
and the operator sequence that follows. Part 1 is Mick's: it is secrets and repository
settings that only the owner can set. Part 2 is the operator's, and none of it works until
part 1 is done.

Nothing here is optional. Each item is a hard dependency of `.github/workflows/daily.yml`;
where a missing item costs a whole day's publication, it says so.

## Part 1: Mick's steps

### 1. Secrets

Settings > Secrets and variables > Actions > New repository secret, on
`mickzijdel/aisafety-radar` (not the data repository).

| Secret | What it is | Used by |
|---|---|---|
| `ANTHROPIC_API_KEY` | A key from a Console workspace with a **$60 per month hard cap** | `canary`, `generate` |
| `DATA_DEPLOY_KEY` | The **private** half of an ed25519 keypair whose public half is a **write** deploy key on `mickzijdel/aisafety-radar-data` | `generate` |
| `HEALTH_ISSUE_TOKEN` | A fine-grained PAT with **Issues: write**, scoped to this repository only | `generate` |
| `HEALTHCHECKS_URL` | The ping URL of a healthchecks.io check, **period 1 day, grace 3 hours** | `promote` |

Details that matter:

- **`ANTHROPIC_API_KEY`.** Put the key in its own Console workspace and set that workspace's
  monthly spend limit to $60. The cap is the last line of defence on cost: the pipeline's own
  budget gates run per day, and only the workspace cap bounds a runaway month. The `canary`
  job runs `tests/test_models_live.py` before anything spends real money, and skips cleanly
  when no key is set, so a fork never blocks on this.
- **`DATA_DEPLOY_KEY`.** Generate with `ssh-keygen -t ed25519 -f radar-data -N ""`. Paste
  `radar-data.pub` into `mickzijdel/aisafety-radar-data` > Settings > Deploy keys, with
  **Allow write access** ticked. Paste the private file `radar-data` (the whole thing,
  including the BEGIN and END lines and the trailing newline) into this secret. Delete both
  local files afterwards.
- **`HEALTH_ISSUE_TOKEN`.** A fine-grained personal access token, resource owner `mickzijdel`,
  repository access limited to `aisafety-radar`, repository permission **Issues: read and
  write** only. The workflow token is not used here on purpose: the health issue is written
  even when the run crashed, and a token that can only touch issues is the smallest thing
  that can do that. Set the longest expiry the UI offers and note the renewal date somewhere
  you will see it: an expired PAT fails exactly the step that costs the whole day's
  publication, and it fails on an otherwise ordinary morning with nothing else wrong.
- **`HEALTHCHECKS_URL`.** Create the check first, then copy its ping URL. Period 1 day and
  grace 3 hours means: alarm if no successful publication has pinged within 27 hours of the
  last one. The ping fires only on a real publication, and a failed ping never fails the run.
  Silence is the alarm.

### 2. Environments

Settings > Environments > New environment.

- **`production`**: wait timer **30 minutes**, **no required reviewers**, no branch
  restrictions needed. This timer is the canary delay: `promote` waits in it while the pack
  sits on the `generated` branch, so a bad pack can be inspected and the run cancelled before
  `main` moves.

  > **The wait timer must stay well under 24 hours.** The `daily` workflow has a
  > workflow-level concurrency group (`group: daily`, `cancel-in-progress: false`), so a run
  > parked in the wait timer holds the slot for the whole workflow. A timer near or over a day
  > means tomorrow's scheduled run queues behind today's, and the schedule silently stops.
  > 30 minutes is the intended value; anything above a few hours is a mistake.

- **`github-pages`**: created by GitHub when Pages is enabled. Check its deployment branch
  rules permit deployments from **`main`**. The `pages` job checks out `main` and deploys
  from it, so a `github-pages` environment restricted to some other branch fails the deploy
  after a successful publication.

### 3. The `radar-health` label

Issues > Labels > New label: **`radar-health`** (any colour, description "pipeline health").

> **Create this before the first run.** The "Update the pinned health issue" step runs
> *before* the publish guard in the `generate` job, and on the first run there is no open
> issue to edit, so it calls `gh issue create --label radar-health`. `gh` fails hard on a
> label that does not exist, the step is not tolerant of that failure, and the whole day's
> publication is lost to a missing label.

### 4. The data repository has at least one commit

`actions/checkout` cannot clone an empty repository, and the `generate` job checks out
`mickzijdel/aisafety-radar-data` into `data/` before it fetches anything. The repository
already carries a README seed commit. Verify:

```bash
gh api repos/mickzijdel/aisafety-radar-data/commits --jq 'length'
```

A number of 1 or more is fine. An error mentioning "Git Repository is empty" means the seed
commit is missing: push any file (a one-line `README.md` is enough) before going live.

### 5. Pages

Settings > Pages > Build and deployment > Source: **GitHub Actions**.

Do not pick "Deploy from a branch". The site is deployed by the `pages` job at the end of the
daily workflow, from the `docs/` directory on `main`, so a failed deploy shows up inside the
day's run rather than as an unrelated red workflow. Note that `docs/` is published whole,
including `docs/superpowers/specs/`: the repository is public, so that is acceptable, but it
is a deliberate choice rather than an accident.

### 6. Branch protection on `main`

Settings > Branches > Add branch ruleset (or classic protection) for `main`:

- **Do not require status checks on `main`.** The `promote` job builds its commit inside the
  runner and pushes it straight to `main`, so no check has ever run for that SHA and a
  required-check rule rejects the push before CI could possibly satisfy it. If the ruleset UI
  lets you scope required checks to pull requests only, that is the right setting; otherwise
  leave required checks off `main` entirely for phase 2. CI still runs on every push and every
  pull request, so a red build is still visible.
- **Leave "restrict who can push" off.** The `promote` job pushes `main` with the workflow
  token, and a push restriction that does not exempt the workflow blocks every publication.
  This is a separate rule from required checks and turning it off does not make required
  checks safe.
- Do not require pull requests for `main` either, for the same reason.
- Safe things to keep on: block force pushes, block deletions, require signed commits **off**
  (the bot does not sign).

> **Watch the first `promote` push specifically.** It is the first time anything pushes `main`
> from a runner, and a branch rule that rejects it fails the job with
> `GH006: Protected branch update failed`. If that happens, remove the required status checks
> from `main` (not the push restriction, which is already off), then **re-run the failed
> `promote` job from that same run** - not the whole workflow. The pack is intact on the
> `generated` branch and `promote` reads it from there, so the rerun costs nothing and publishes
> the pack that was already generated. Re-running `daily.yml` instead would re-generate the day
> (a second bill) and, worse, find every one of the day's items already marked seen by the
> generation that succeeded, so it would publish an empty pack. Spec 6.9 says the same thing.

### 7. Tell the operator

Once 1 to 6 are done, say so. Part 2 needs all of them.

## Part 2: the operator sequence

Run in order. Stop at the first failure and fix it before continuing.

1. **Probe every source from a runner.**

   ```bash
   gh workflow run probe.yml
   sleep 5 && gh run watch \
     "$(gh run list -w probe.yml -L1 --json databaseId --jq '.[0].databaseId')" --exit-status
   ```

   The run id is resolved rather than left to a bare `gh run watch`, which picks the most
   recent run in the repository and may well be something else; the `sleep` gives the
   dispatched run time to appear in the list at all.

   `radar probe` always exits 0 on purpose: a blocked source has to show up as a row in the
   job summary, not as a traceback. Copy the summary table into `docs/probe-results.md` with
   the date. For every source marked `NO`, read the error:
   - a Cloudflare 403 or challenge page: record it in the spec's 5.1 table and open the
     "self-hosted runner or Tailscale proxy" question in `.claude/current_plan.md`;
   - a GraphQL field error (for example `jobTitle`): remove the field from `USER_FIELDS`,
     rerun the local tests, commit, and rerun the probe.

2. **The first real daily run.**

   ```bash
   gh workflow run daily.yml
   sleep 5 && gh run watch \
     "$(gh run list -w daily.yml -L1 --json databaseId --jq '.[0].databaseId')" --exit-status
   ```

3. **Eyeball the pack during the canary window.** When `generate` has passed and `promote` is
   waiting on the `production` timer, read
   `skills/aisafety-radar/references/31-today-forums.md` on the **`generated`** branch and
   check by eye: real titles, links that open, summaries that match the posts, quotes that
   appear in the source text, and no movers (the ledger has no previous day on day one). If
   anything is wrong, cancel the run before the timer ends. Cancelling costs nothing that has
   already been published.

4. **Watch the `promote` job's "Commit and push the publication" step.** This is the first
   time anything has pushed `main` from a runner, and it is the step a branch rule rejects
   (`GH006: Protected branch update failed`). If it fails there, remove the required status
   checks from `main` (see part 1 step 6) and re-run **the failed `promote` job only**
   (`gh run rerun <run-id> --failed`). Do not rerun the whole workflow: see "an unpublished
   day" below for why that publishes nothing.

5. **Verify the published site.** After `pages` completes, open
   `https://mickzijdel.github.io/aisafety-radar/latest/00-index.md` and confirm it is no
   longer the placeholder. Then, locally:

   ```bash
   skills/aisafety-radar/scripts/fetch-latest.sh 31-today-forums.md | head
   ```

   It must print the file's first lines. The script verifies the file's sha256 against
   `latest/checksums.txt` first and exits non-zero without printing on a mismatch, so a
   successful print is also a checksum check.

6. **Install the plugin and smoke-test the skill.**

   ```bash
   claude plugin marketplace add mickzijdel/aisafety-radar
   ```

   Install `aisafety-radar`, start a session, and ask "what happened on the AI safety forums
   today?". The skill should trigger, read `references/00-index.md`, load only
   `31-today-forums.md`, and answer with links. Note anything that misfires in
   `.claude/current_plan.md` as input to the phase 3 plan.

7. **Watch the first scheduled run.** The cron is `23 5 * * *` UTC, deliberately off the hour
   because GitHub's scheduler delays popular minutes most. After it, read the pinned health
   issue (label `radar-health`) and confirm the healthchecks.io check went green.

## When something goes wrong

- **`generate` failed red at "Update the pinned health issue", nothing published and no health
  issue exists.** The `radar-health` label is missing, or `HEALTH_ISSUE_TOKEN` lacks Issues:
  write on this repository, or it has expired. The `gh` call is checked, so this is a hard
  step failure rather than a quiet skip: the day is lost, and the one artefact that would have
  explained why is the thing that failed to be written.
- **`generate` failed at "Check out the data repository".** `DATA_DEPLOY_KEY` is wrong, is
  missing its trailing newline, or its public half is not a write deploy key on the data repo.
- **`generate` succeeded but `promote` never started.** The run did not publish: read the
  health issue, which names the blocking gate or the refusal reason.
- **`promote` is stuck.** That is the wait timer. Check the `production` environment's timer
  is 30 minutes and not something larger.
- **`promote` failed at "Commit and push the publication" with `GH006: Protected branch update
  failed`.** A branch rule on `main` rejected the push. Almost always required status checks:
  the pushed SHA was built in the runner and no check has run for it. Remove required checks
  from `main` (part 1 step 6), then re-run **that run's failed jobs**, not the workflow:

  ```bash
  gh run rerun "$(gh run list -w daily.yml -L1 --json databaseId --jq '.[0].databaseId')" --failed
  ```

  The pack is intact on the `generated` branch, which is where `promote` reads it from, so the
  rerun publishes the pack that was already paid for.
- **An unpublished day: `generate` succeeded, `promote` did not, and the run is gone.** The
  generation marked the day's items seen when its gates passed - one job before `promote` - so a
  plain rerun of `daily.yml` fetches the same posts, filters every one of them out as already
  seen, and publishes an empty pack (the "Refuse an empty or blocked pack" step stops it, which
  is the day lost a second time). If re-running the old run's `promote` job is no longer
  possible, dispatch the workflow with the recovery input instead:

  ```bash
  gh workflow run daily.yml -f ignore_seen=true
  ```

  That skips the seen-ids filter for one run and regenerates the day from scratch. It costs a
  second full generation - another day's API spend - so it is the fallback, not the first move.
- **`pages` failed after a good publication.** The `github-pages` environment does not allow
  deployments from `main`, or Pages is still set to "Deploy from a branch".
- **The schedule stopped firing.** Check for a run parked in the `production` wait timer
  holding the `daily` concurrency group.
