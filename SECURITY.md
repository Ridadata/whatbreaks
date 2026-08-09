# Security

## Reporting

Please report vulnerabilities through
[GitHub Security Advisories](https://github.com/Ridadata/whatbreaks/security/advisories/new)
rather than a public issue.

## Threat model

whatbreaks runs in CI, on pull-request content that anyone may have authored.
That is a design constraint, not an afterthought.

| Threat | Mitigation |
| --- | --- |
| Malicious SQL or Jinja achieving code execution | SQL is never executed. Jinja renders in a `SandboxedEnvironment` with no filesystem loader, a bounded `range()` so a generated template cannot hang a job, and an output size cap |
| Secret exfiltration | **Zero network calls**, enforced by a test that blocks socket creation. No credentials are read; `profiles.yml` is never parsed for anything |
| Markdown / HTML injection into a PR comment | Model and column names are attacker-controlled. All interpolated text is entity-encoded and markdown-escaped, `@` is neutralised so a model name cannot page an organisation, and output length is capped |
| Malicious `manifest.json` | Treated as untrusted: schema version validated, node count and string lengths capped, path traversal in `original_file_path` refused |
| Supply chain | Four runtime dependencies. Lockfile committed, Dependabot enabled, PyPI publishing via Trusted Publishing (OIDC, no long-lived token) |

## The fork-safe pattern

whatbreaks needs no secrets, which is what lets it run on pull requests from
forks — where `GITHUB_TOKEN` is read-only and secrets are withheld.

When the GitHub Action ships it will use the two-workflow pattern: analysis runs
on `pull_request` with a read-only token and uploads an artifact; a separate
`workflow_run` job posts the comment without checking out untrusted code.

**It will never use `pull_request_target` with a checkout of the pull request's
head.** That combination is the documented "pwn request" vulnerability class,
and it hands repository secrets to code an attacker controls.
