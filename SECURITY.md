# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately through the coding-hermes GitHub organization’s private security-reporting channel for this repository. If that channel is unavailable, contact the repository maintainers through a private channel listed on the repository page. Do not include exploit details, production URLs, credentials, tokens, or customer data in a public issue.

A useful report includes:

- a concise description of the impact and affected component;
- reproducible, non-destructive steps or a minimal proof of concept;
- affected versions or commit range, if known;
- suggested mitigations, if available; and
- a safe contact method for follow-up.

Maintainers will acknowledge receipt, assess impact, coordinate a fix, and publish an advisory when disclosure is appropriate.

## Key-handling policy

- Use environment-variable references or a local, ignored secret manager for credentials.
- Never commit API keys, bearer tokens, passwords, OAuth refresh tokens, private keys, cookies, authorization headers, or real `.env` files.
- Do not put secrets in source code, tests, fixtures, documentation, examples, commit messages, issue comments, logs, shell history, or generated registry/state files.
- Use placeholders such as `PROVIDER_API_KEY` in examples. A placeholder must not resemble or contain a usable credential.
- Revoke and rotate any credential that may have reached a branch, tag, artifact, issue, or log before attempting history cleanup.

## What is a secret here

Treat the following as sensitive even when they are not labeled “secret”:

- provider access tokens, API keys, service-account material, and signed URLs;
- internal endpoints, private hostnames, IP addresses, and non-public repository locations;
- routing state that exposes account balances, quotas, request metadata, or customer/project identifiers;
- production logs, ledger rows, health reports, and configuration snapshots; and
- unpublished vulnerability details or bypass techniques.

The committed `data/tables/*.jsonl` catalog must contain only public, reviewable model and provider metadata. Local runtime files such as `registry.json`, circuit state, health state, and ledgers must be treated as deployment data, not sample data, unless deliberately sanitized.

## Supported versions

Security fixes are applied to the maintained default branch. If you run a fork or pinned revision, update to a fixed revision after an advisory is released.
