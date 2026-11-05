# Contributing to Smart OA Agent

Thank you for helping improve Smart OA Agent. The project is currently intended for learning, demonstrations, and small closed tests, so changes should remain easy to review and reproduce.

## Before You Start

1. Check existing issues before opening a new one.
2. For a substantial behavioral change, describe the use case and proposed scope in an issue first.
3. Never include real credentials, personal data, production logs, database exports, or confidential approval content.

## Development Workflow

1. Fork the repository and create a focused branch.
2. Copy the provided `.env.example` files and keep local `.env` files untracked.
3. Make the smallest coherent change needed for the issue.
4. Add or update tests and documentation when behavior changes.
5. Run the relevant verification commands before submitting a pull request.

Backend verification:

```bash
cd backend
python -m unittest discover -s tests -v
```

Frontend verification:

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm run build
```

Compose verification from the repository root:

```bash
docker compose config --quiet
docker compose ps
```

## Pull Requests

A pull request should:

- explain the problem and the chosen solution;
- list the verification performed;
- call out migrations, configuration changes, or compatibility risks;
- include sanitized screenshots for visible UI changes;
- avoid unrelated formatting or refactoring;
- keep generated directories such as `node_modules`, `dist`, caches, logs, and local environment files out of Git.

Before committing, inspect the staged content:

```bash
git diff --cached --stat
git diff --cached
```

Confirm that no secrets, temporary passwords, local paths, dumps, or real user data are staged.

## Code and Service Principles

- Preserve backend authorization checks; hiding a frontend control is not authorization.
- Keep request IDs and structured error handling intact.
- Avoid silently changing database initialization or migration behavior.
- Do not add default administrator passwords or demo credentials.
- Keep public examples provider-neutral and use placeholders for external services.
- Document operational changes in the relevant README.

## Reporting Security Problems

Do not disclose vulnerabilities or credentials in a public issue. Follow [SECURITY.md](SECURITY.md) for private reporting guidance.
