# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately**, not as a public GitHub issue —
through GitHub's built-in private reporting:

1. Go to the [Security tab](https://github.com/thameema/memnos/security).
2. Click **Report a vulnerability**.

This opens a private advisory visible only to you and the maintainer, so the issue isn't
disclosed publicly before a fix is available. You'll get an acknowledgment and, once a fix
ships, credit in the advisory (unless you'd rather stay anonymous).

## Supported versions

memnos ships frequent releases with no long-term-support branch — only the
[latest release on PyPI](https://pypi.org/project/memnos/) receives security fixes.
Run `memnos upgrade` to stay current.

## Scope

In scope: the `memnos` server/CLI, the `memnos-sdk` client, and the `tommy-orchestrator`
package in this repository. Vulnerabilities in dependencies should be reported upstream,
though we'd still like to know about anything that affects memnos directly (e.g. an
exploitable path through how we use a dependency).

Of particular interest: anything touching authentication (token handling, namespace ACL
enforcement), the encrypted secret vault, or SQL injection in the recall/write paths.
