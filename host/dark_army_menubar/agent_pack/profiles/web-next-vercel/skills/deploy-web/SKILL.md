---
name: deploy-web
description: Runbook for shipping the {{PROJECT}} web app — what CI checks,
  what a pull request preview does, what a push to main sets in motion
  (migrate, then deploy), how to confirm it landed, and the rules that keep a
  deploy from breaking the still-live version. Use when the user says
  /deploy-web, "deploy", "ship it to production", "did the deploy work", or
  before touching the env schema or a migration.
---

# deploy-web

**Pushing to the default branch IS the deploy.** `.github/workflows/deploy.yml`
fires on every push to `main`. Nothing else is required, and nothing in this
skill runs a deploy by hand. The pipeline never commits or pushes; deploying
is the user's push.

## What the workflows do

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | every PR and push (web paths) | lint → typecheck → test → build with **placeholder** env values |
| `deploy.yml` preview | PR opened / updated | creates a **branch database** for the PR, migrates it, deploys a Vercel preview, comments the URL |
| `deploy.yml` cleanup | PR closed | deletes that branch database |
| `deploy.yml` production | push to `main` | **migrate production first**, then `vercel pull && vercel build --prod && vercel deploy --prebuilt --prod` |

The production order is the invariant: migrations run against the still-live
old code, so **migrations are additive**. A drop, rename or NOT NULL is split
across two deploys: stop writing → deploy → drop.

## Rules

- **Never run `vercel --prod` by hand.** The workflow is the only deploy path;
  a hand deploy skips the migration and leaves no run to audit.
- **Verify with `gh run list --limit 4`** and expect both `CI` and `Deploy`
  green. Allow a couple of minutes. **`vercel ls` immediately after a push is
  stale** and shows the previous deployment — do not conclude from it that
  nothing deployed.
- **There is no Vercel-native Git integration**, so the dashboard lists
  deployments by username rather than commit. That is expected.
- **Secrets are configured in two places** and stay there: the Vercel project
  env for the app, GitHub repo secrets for the workflow. Do not ask the user
  to re-add them.
- **Before a deploy that touches the env schema:** every required field must
  exist in the Vercel project for Production **and** Preview, and be mirrored
  into `ci.yml`'s placeholder block. The schema is validated at **build**
  time, so a missing var fails the build rather than degrading.
- **Confirm the deploy by fetching a real page** on the production URL from
  `docs/context.md`. Raw `<project>-<hash>-…` URLs may 302 to Vercel SSO,
  which is deployment protection, not failure.

## When asked to deploy

1. Confirm the working tree is committed on `main` — if not, say so and stop;
   committing is the user's act.
2. Say what the push will set in motion, in one sentence.
3. After the user pushes, run `gh run list --limit 4` until `CI` and `Deploy`
   are both green or one is red. On red, `gh run view <id> --log-failed` and
   report the failing step in plain words.
4. Fetch the production page and report that it renders.

## Secrets and variables the workflows expect

| Where | Name | Used by |
|---|---|---|
| GitHub secret | `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` | preview and production deploy |
| GitHub secret | `DATABASE_URL` | production migrate |
| GitHub secret | `NEON_API_KEY` | preview branch create/delete |
| GitHub variable | `NEON_PROJECT_ID` | preview branch create/delete |
| Vercel project env | every field in the env schema, Production **and** Preview | the app |

Prefer several small conventional commits with real reasoning in the message
over one blob — the history is bisectable and the *why* is what a future
reader needs.
