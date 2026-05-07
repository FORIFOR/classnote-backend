# Rollback-Protected Fixes

This file lists commits / changes that **must NOT be lost** by a rollback.
Production Manager Agent reads this before approving any rollback. If a
candidate rollback discards any of these, switch to **roll-forward hotfix**.

Format:
```
- <commit-sha>  <release-unit / regression id>  <category>  <one-line description>
```

Categories: `billing` `auth` `security` `data` `plan` `migration`

---

## Active protections (do not lose by rollback)

- `ff1bce1c`  REG-BE-017  plan       trust entitlement.status over stale currentPeriodEnd (subscribers were downgraded to free; fix lives in `app/routes/users.py`)
- `c84b29af`  —           data       restore `/internal/tasks/summarize_quick` worker (404 → 200; quick summary feature back)
- `00261-njw` rev          data       summary idempotency: stop pre-writing `idempotencyKey` in `/sessions/{id}/jobs` so Cloud Tasks worker no longer skips
- `00263-lxw` rev          plan       `planRepairExempt` safeguard in `_repair_account_plan` (master + F459bssBVwSp0cqlAbN8 protected from auto-downgrade)
- env vars               billing    Apple/Stripe/LINE/Slack/Microsoft/Google OAuth secrets and signing keys (loss of any breaks login or webhooks)

## Past incidents that justified entries above

- 2026-05-01: 6 paid subscribers downgraded to free because verifier looked at `currentPeriodEnd < now` ignoring `status=active` (webhook lag) — fix `ff1bce1c`.
- 2026-05-04: master account auto-downgraded twice in one day due to old revision still serving on `dev` / `candidate` tags — fix in `00263-lxw` plus `update-traffic --update-tags ...` to point tags at latest.
- 2026-05-04: Master's session `704B0BD4-...` stuck at `summaryStatus=running` because `/sessions/{id}/jobs` pre-wrote the idempotencyKey, causing the Cloud Tasks worker to short-circuit on `idempotent_hit` — fix in `00261-njw`.

## Rollback ban rules

You **MUST NOT** rollback if the candidate diff loses any commit categorised as:
- `billing` / `plan`
- `auth` / `security`
- `data` / `migration`

In those cases:
1. cherry-pick / re-implement the lost fix on top of the rollback target
2. deploy as a new revision (roll-forward hotfix)
3. update this file with the new commit / revision

## Lost Fixes Review checklist

Before approving rollback `revA` → `revB`:
1. `git log <revB-commit>..<revA-commit> --oneline -- app/`
2. For each commit, classify by category above
3. If any is in protected category → switch to roll-forward
4. Otherwise log the rollback rationale + verified scope of loss in `docs/incidents/<date>-<slug>.md`
