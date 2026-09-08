# aisafety-radar: current plan

Plan: `docs/superpowers/plans/2026-08-30-phase-1-2-forums-vertical-slice.md`
Spec: `docs/superpowers/specs/2026-08-30-aisafety-radar-design.md` (revision 2, binding)
SDD ledger: `.superpowers/sdd/2026-08-30-phase-1-2-forums-vertical-slice/progress.md`, with a
`task-N-brief.md` and `task-N-report.md` per task and the review diffs beside them. Every
ruling, deferred minor and must-fix-in-the-final-loop item is recorded there; read it before
touching anything from phases 1 and 2.

Worktree: `.worktrees/phase-1-2-forums`, branch `phase-1-2-forums`, base `509160c`.

## Status

Phases 1 and 2 (skeleton and probe, forums vertical slice): **tasks 1 to 19 complete**, each
reviewed and closed clean in the ledger. Task 20's file work is done too (this checkpoint, the
README install matrix, `docs/go-live.md`).

What that means concretely: LW/AF/EAF fetchers with the entry floor and comment refresh,
triage, extraction with the substring verifier and URL provenance check, the injection scan, the
seven QA gates, the `31-today-forums.md` render, versioning, the publishing helpers (changelog,
manifests, health issue, site) and the `daily`, `promote`, `pages`, `probe` and `ci` workflows.
Nothing has run against the live API or a real runner yet, and nothing is published.

**Not** the roundup split. Spec 6.3 makes it the first step of stage 1 and this checkpoint used
to claim it was done; it does not exist in code or tests. It is deferred to phase 3 by ruling
(final loop, wave 4), where the roundup sources it was designed for actually land. The spec now
records that as a deliberate deviation in 6.3, and phase 2 treats a roundup as a single item.

Task 20 was restructured by ruling: its live steps need repository secrets only Mick can set,
so they run after the merge rather than as part of the SDD task sequence.

## Next steps, in order

1. **Final review-fix-review loop**: at least 5 rounds, up to 8 (Mick's standing instruction
   of 2026-08-31, superseding the single review plus one fix wave). Start from the
   MUST-FIX-in-final-loop items already collected in the ledger, notably: SKILL.md's
   "script fails" clause must distinguish a checksum mismatch (use the bundled copy and say
   so) from an unreachable host (try the URL fetch); a scan-flagged record must not reappear
   as a discussion mover; spec 4.3's agreement line corrected to the net form. Then the
   deferred minors, including the `jq -e` regression test for `daily.yml`, `refusal_reason`'s
   fail-open on a missing `dropped` key, and the `ci.yml` `uv sync` vs `--locked` drift.
2. **Merge `phase-1-2-forums` into `main`** and clean up the worktree
   (`superpowers:finishing-a-development-branch`). CI has never executed; confirm it goes
   green on the first push.
3. **Mick's go-live checklist**: part 1 of `docs/go-live.md`. Secrets
   (`ANTHROPIC_API_KEY` on a $60/month capped workspace, `DATA_DEPLOY_KEY`,
   `HEALTH_ISSUE_TOKEN`, `HEALTHCHECKS_URL`), the `production` environment with a 30-minute
   wait timer, the `radar-health` label, the data repo's seed commit, Pages set to GitHub
   Actions, and branch protection on `main` with **no required status checks** and push
   restrictions left off (the `promote` job pushes a SHA built in the runner that no check has
   ever run against; spec 6.9 now says this rather than implying the opposite).
4. **The live steps**: part 2 of `docs/go-live.md`. Run `probe.yml` and record the table in
   `docs/probe-results.md` (that file does not exist yet, and the workflow does not create it:
   `radar probe` writes only to `$GITHUB_STEP_SUMMARY`, and the operator copies the table out
   of the job summary by hand, as `docs/go-live.md` part 2 step 1 says); run
   `daily.yml`; eyeball the pack on the `generated` branch during the 30-minute canary
   window; watch `promote`'s push step (the first push of `main` from a runner); verify Pages
   and `fetch-latest.sh`; install the marketplace plugin and smoke-test the skill.

   If `promote` fails, re-run **that run's failed jobs**, not the workflow: the day's items are
   already marked seen (the gates pass in `generate`), so a fresh `daily.yml` run publishes an
   empty pack. `gh workflow run daily.yml -f ignore_seen=true -f day=YYYY-MM-DD` regenerates a
   spent day at the cost of a second generation, and is the fallback when the old run can no
   longer be rerun. `day` has to be the day being recovered, in UTC: the exact window is on
   record in that day's own `run.json` and is reused only for that day, and a dispatch without
   it dates itself today, fetches a disjoint window and selects nothing.
5. **Write the Phase 3 plan**: news, labs, the governance set, papers, the X-Risk Daily
   import, appendices `30`, `32`, `33` and `60`, and the bundles. Feed into it whatever the
   probe found and anything that misfired in the skill smoke test. Phase 3 also owns two things
   phase 2 deliberately did not build, and both need to be first-class items in that plan rather
   than assumptions:
   - **The roundup split** (spec 6.3). Cut a roundup at its headings into sub-items carrying the
     parent URL plus the anchor and any outbound link named. It belongs with Zvi and the weekly
     briefings, which arrive in phase 3; until then a roundup is one item with one summary.
   - **Top comments per selected item** at extraction time (spec 6.3), which is why every fresh
     item's `discussion_signal` is null on the day it appears and is filled in only by the next
     morning's mover refresh.

Phases 4 to 6 (rolling views and structured feeds; map, careers and field context;
portability) each need their own plan once phase 3 is written. Spec section 14 has the
phasing.

## Open decisions for Mick

- Whether any forum source blocked from GitHub runners justifies a self-hosted runner or a
  Tailscale proxy. The probe run answers this.
- `docs/` is published whole by the `pages` job, so `docs/superpowers/specs/` goes to the
  public site. Acceptable for a public repo, but it is a choice.
