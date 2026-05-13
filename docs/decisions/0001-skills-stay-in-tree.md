# ADR 0001: Keep Bundled Skills In Tree

## Status

Accepted

## Context

Syll skills are markdown packages discovered from `syll/skills/` and the user
workspace. They are intentionally low-coupling: a bundled skill can usually be
added, reviewed, or removed without changing core runtime code.

Because skills are easy to contribute, they could eventually become a separate
community repository. At v0.2.x, however, bundled skills are also part of the
first-run onboarding experience: installing `syll` should provide enough working
examples for users to understand how the companion can be extended.

## Decision

Keep bundled skills in the main repository for the v0.3.x cycle. Do not create a
separate `syll-skills` repository yet.

## Consequences

Users continue to get core examples with `pip install syll`. Contributors can
submit small Skill PRs without learning a separate repository or release flow.
The main repository remains the source of truth for examples that define the
expected skill format.

Revisit this decision if external Skill PRs reach five or more per month, if a
community emerges that wants to reuse Syll-compatible skills outside Syll, or as
part of the v0.4.0 planning review.
