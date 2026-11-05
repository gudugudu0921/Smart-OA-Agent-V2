# Security Policy

## Supported Scope

Smart OA Agent is currently a learning, demonstration, and small closed-test project. It has not received an independent security audit and should not be treated as production-ready software.

Security fixes are applied to the latest code on the default branch. Historical versions are not maintained as separate supported release lines.

## Reporting a Vulnerability

Please do not publish credentials, personal data, complete exploit details, or sensitive logs in a public issue.

If the repository enables GitHub private vulnerability reporting, use a private security advisory. Otherwise, open a minimal public issue that only states that a security problem exists and asks the maintainer to provide a private reporting channel. Do not include the exploit, secrets, or affected user data in that issue.

When reporting privately, include:

- the affected version or commit;
- the affected endpoint or component;
- reproducible steps with sanitized data;
- the expected and observed behavior;
- the potential impact and any suggested mitigation.

Please report security vulnerabilities privately and avoid disclosing sensitive details before a fix is available.

## Secrets and Configuration

- Never commit `.env` files, API keys, JWT signing secrets, database passwords, Redis passwords, access tokens, private keys, database dumps, production logs, or real user data.
- Commit only sanitized examples such as `.env.example`.
- Use long, unique values for `JWT_SECRET_KEY`, database credentials, Redis credentials, and external model-provider credentials.
- Rotate a credential immediately if it is exposed in a commit, log, screenshot, issue, or chat. Removing it from the latest file is not enough because Git history may retain it.
- Keep backups outside the repository and protect them with access controls and encryption appropriate to their contents.

## Bootstrap Administrator

The bootstrap command prints a temporary administrator password once. Treat that output as a secret:

- run bootstrap only from a trusted terminal;
- do not copy the temporary password into documentation, screenshots, issues, or logs;
- sign in promptly and complete the required password change;
- do not reuse the temporary password for another account or environment.

## Deployment Guidance

Before any internet-facing deployment:

- terminate traffic with HTTPS and use a trusted certificate;
- restrict database and Redis access with a firewall or private network;
- replace all example values and development defaults;
- limit administrator accounts and review audit records regularly;
- configure backup, restore testing, monitoring, and log retention;
- review the external LLM provider's data handling and avoid sending confidential material without an approved policy;
- assess browser token storage and session protections for the intended threat model.

The supplied Docker Compose setup is designed for local evaluation and controlled testing. It is not, by itself, a hardened production deployment.

## Logs and Test Data

Request IDs and structured logs help troubleshooting, but logs can still expose business context. Sanitize logs before sharing them and never attach full environment files or database exports to an issue. Use fictional users and non-sensitive approval content during testing.
