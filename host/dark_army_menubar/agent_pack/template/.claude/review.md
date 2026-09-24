# Review profile — {{PROJECT}}

Read by the `/review` skill. Names this project's reviewer and what "risky"
means here, so a review is about *this* codebase rather than generic.

```yaml
reviewer_agent: {{REVIEWER_AGENT}}
gitnexus_repo: {{GITNEXUS_REPO}}
```

**`gitnexus_repo` is the name GitNexus indexed this project under** — the git
remote's name, which is often not the folder name. A GitNexus call made
against the wrong name finds nothing and looks exactly like a clean review.
`npx gitnexus analyze` prints the name it used; `list_repos` confirms it.

## Risk domains

These are the project's own conventions (`docs/context.md`). A change that
weakens one is a finding even when the tests pass.

{{RISK_DOMAINS}}

## Notes for the reviewer

- `never_ship_without`: a test that fails before the fix, for anything in the
  hermetic half; a stated manual check, written as steps, for the rest.
- The house style is **fail closed**: ambiguity refuses rather than guesses. A
  change that makes an ambiguous case resolve to "first match" is a finding.
