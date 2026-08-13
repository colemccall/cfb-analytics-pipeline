# Build log — spec for `changes.html`

*A task brief. Hand this to Claude Code as its own session, before any backlog work.*

## Why this exists

`methods.html` explains what a number **is**. `research-*.html` explains what a
finding **claims**. Neither explains what happened in a working session — which
alternative was tried and abandoned, what the measurement said, why a constant
moved. That record currently lives in commit messages, in four markdown
changelogs, and in code comments. None of it is reachable from the site.

The consequence is the one this project exists to avoid: **the site asserts
things it never justifies.** A rating changed between two visits and nothing
forward-facing says why.

This is the same problem `methods.html` solved for formulas, so it gets the same
solution: a registry, a shell, and no page code.

## What to build

Three files, mirroring the `methods.js` / `methodsPage.js` / `methods.html`
split exactly. Do not invent a new pattern.

```
js/changes.js       # the CHANGES registry — content only
js/changesPage.js   # rendering — presentation only
changes.html        # ~25-line shell, calls initChanges()
```

Plus a nav entry in `js/shell.js`, and a `short` label for the mobile tab bar.

### Entry contract

Deliberately a subset of `METHODS`, so the two can converge later:

```
id          registry key + anchor              "v4.4-usage"
version     rating/release version             "v4.4"
date        ISO date shipped                   "2026-08-20"
title       what this pass was about
summary     one paragraph — the problem, not the solution
motivation  what prompted it: a complaint, a measurement, a bug
did         [{ what, why, cost }]              what changed and what it traded away
tried       [{ what, result, verdict }]        alternatives tried and abandoned
measured    [{ label, before, after }]         numbers, before and after
gates       [{ check, result }]                what was run to prove it
unfixed     what this pass did NOT fix — required, never empty
methods     [ids] — METHODS entries this pass changed, rendered as anchors
files       new / substantially changed
```

Two fields carry the weight and both are **required**:

- **`tried`** is the point of the whole page. Havoc share, the rejected
  best-skill-plus-partial-credit combination, full quantile mapping — each was
  built, measured, and dropped, and each is invisible to a reader today. An
  entry with an empty `tried` array means either nothing was explored or the
  exploration was not recorded. Both are worth surfacing.
- **`unfixed`** mirrors the "what this pass did not fix" section every changelog
  here already ends with. Keep the habit; make it reachable.

### Rendering notes

- Reverse chronological, newest first. Version + date in the masthead of each
  entry.
- `tried` rows use the existing `method-chip` statuses (`rejected`,
  `experiment`, `shipped`) — no new chip system.
- `measured` renders as the existing `.method-evidence` table with a
  before/after column pair.
- `methods` ids render as links to `methods.html#<id>`, so a reader can go from
  "what changed" to "what it is" in one click. Validate at render time that the
  id exists in `METHODS` and render plain text if not — a dead anchor is worse
  than no link.
- Escape by default, same `_mEsc` / `_prose` helpers. Do not duplicate them;
  either import from `methodsPage.js` or extract both into `ui.js`. Prefer
  extracting — two copies will drift.

### Seeding

Backfill from the four existing changelogs: `v3.1`, `v3.2`, `v3.3`, `v4.3`.
They already contain every field this contract asks for, including `unfixed`.
This is transcription, not authorship — do not reinterpret past decisions, and
where a changelog is vague, say so in the entry rather than inventing a number.

### Going forward

Per `CLAUDE.md` §2: a `CHANGES` entry ships in the same commit as the work it
describes. Not after. Not batched at release time — batching is how the
reasoning gets lost, because the reasoning is freshest in the session that
produced it.

## Definition of done

- `node --check js/changes.js js/changesPage.js`
- `node tools/contrast-check.mjs` still green
- Page renders in both themes at 1440px and ≥500px (remember: headless Chrome
  clamps the layout viewport near 500px — 390px screenshots show a false clip)
- Every seeded entry has non-empty `tried` and `unfixed`
- Every `methods` id resolves to a real `METHODS` entry
- Nav entry present with a `short` label; mobile tab bar does not overflow
- A `METHODS`-style entry is **not** required for this change — but a `CHANGES`
  entry for building the changes page is, and it should be the first one written
  by hand rather than backfilled.
