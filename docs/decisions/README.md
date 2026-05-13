# Architecture Decision Records

Syll uses lightweight Architecture Decision Records (ADRs) for changes that
alter long-lived project contracts. ADRs should be short and practical: enough
context for future maintainers to understand why a direction was chosen.

## When an ADR is required

Create an ADR when a pull request:

- changes public semantics in `syll/agent/`, `syll/bus/`, or `syll/session/`
- changes the meaning, default, migration behavior, or security boundary of an
  existing field in `syll/config/schema.py`
- introduces a new top-level package or runtime subsystem
- changes how third-party tools execute code, access files, or expose secrets

## When an ADR is not required

An ADR is not required for:

- new Skills
- ordinary Tool, Channel, or Web route additions
- local schema additions for a new Channel or Tool
- bug fixes that preserve existing contracts
- tests, documentation, CI, or release infrastructure

Use `0000-template.md` for new records. Number records sequentially and keep the
status clear: `Proposed`, `Accepted`, `Deprecated`, or `Superseded`.
