# Security Policy

## What we consider a vulnerability

The scan API fetches and checks attacker-supplied URLs. A vulnerability is
anything that lets a caller:

- make the service read or write outside its documented surface (repo
  tarball of a *public* GitHub repository, outbound HTTP checks),
- trigger unbounded work from a single request (resource exhaustion beyond
  the documented `max_files` / `max_urls` / timeout caps),
- obtain information the API does not intentionally return,
- execute code on the host.

## Reporting

Please use **[private vulnerability reporting](https://github.com/auroraxo/docrot-api/security/advisories/new)**
or open a private contact through [the owner profile](https://github.com/auroraxo).
Do not file public issues for security problems.

We aim to acknowledge within **3 business days**. This service is operated by
an autonomous agent under human ownership; reports are routed to the human
owner for anything beyond documentation-level fixes.

## Context: our own bug classes

This codebase treats false-positive *checking* behavior as a defect class and
has shipped fixes for it (v1.1.0 HTML-entity decode, v1.2.0 code-example
exclusion). A bug in URL extraction or liveness checking that returns wrong
results to paying customers is a quality issue, not a security issue — unless
it falls into one of the categories above.

## Scope note

Pilot phase: manual invoicing, no automated billing, no stored secrets in the
service path. The service runs stdlib-only Python behind a reverse proxy.
