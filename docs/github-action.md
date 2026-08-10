# Running whatbreaks in GitHub Actions

Two workflows, not one. The reason is worth understanding before you copy
anything, because the obvious single-workflow version is a known vulnerability.

---

## Quick start

Copy both templates into `.github/workflows/`:

- [`whatbreaks.yml`](../.github/workflows/templates/whatbreaks.yml) — analysis
- [`whatbreaks-comment.yml`](../.github/workflows/templates/whatbreaks-comment.yml) — posts the comment

Adjust the dbt adapter in the first one. That is usually the only edit needed.

## Why two workflows

A `pull_request` run **from a fork** gets a read-only `GITHUB_TOKEN` and no
secrets. GitHub does this deliberately: that job executes code the contributor
wrote. It follows that the analysis job *cannot post its own results*.

The obvious fix is to switch the trigger to `pull_request_target`, which runs
with a write token and full access to repository secrets. Combined with checking
out the pull request's code, that is the documented **"pwn request"**
vulnerability class — you have handed your secrets to code an attacker controls.

So the work is split by trust level:

| | `whatbreaks.yml` | `whatbreaks-comment.yml` |
| :--- | :--- | :--- |
| Trigger | `pull_request` | `workflow_run` |
| Runs contributor code | **yes** (`dbt parse` renders their Jinja) | **no** |
| Token | read-only | `pull-requests: write` |
| Secrets | none | repository secrets available |
| Checks out the PR | yes | **never** |
| Posts anything | no | yes |

The untrusted job holds nothing worth stealing. The trusted job never runs
anything the contributor wrote — it downloads an artifact, validates it as
untrusted data, and posts it.

whatbreaks is unusual in being able to work this way at all: it needs no
warehouse credentials, so the analysis job has no secrets to protect.

## Treating the artifact as untrusted

The comment job reads an artifact produced by a job that ran someone else's
code, so the template validates before using anything:

- the PR number must match `^[0-9]+$` — it is interpolated into a `gh` command
- the report must be non-empty
- the body is truncated, so a generated project cannot post a page-breaking comment

The report content itself is already hardened at the source: the markdown
reporter entity-encodes angle brackets, neutralises `@` so a model named
`@everyone` cannot page an organisation, and strips backticks that would close a
code span early.

## Single-workflow mode

If you do not accept pull requests from forks, you can skip the second workflow:

```yaml
- uses: Ridadata/whatbreaks/action@v0.2.0
  with:
    base-manifest: .base/target/manifest.json
    head-manifest: target/manifest.json
    comment: "true"          # needs pull-requests: write on the job
```

The action warns rather than failing if the comment cannot be posted, so a fork
PR still produces a job summary instead of a red X for the wrong reason.

## Results are never invisible

The action always writes the report to `$GITHUB_STEP_SUMMARY`, which renders on
the run page. On a fork PR — where nothing can be posted to the conversation —
that is how the contributor sees the result at all.

## Producing the two manifests

whatbreaks compares two `manifest.json` files. It does not run dbt for you: your
adapter, packages and vars are yours, and guessing at them is how a tool becomes
brittle.

`dbt parse` is enough. It runs offline and needs no warehouse connection, though
it does insist on a profile existing — point `DBT_PROFILES_DIR` at a dummy one
rather than putting real credentials in a workflow a fork can read. There is
nothing to connect to and nothing to leak.

```yaml
- uses: actions/checkout@v4
- uses: actions/checkout@v4
  with:
    ref: ${{ github.event.pull_request.base.sha }}
    path: .base
```

Passing `base-root` and `head-root` is optional but improves resolution: they
unlock seed CSV headers and `dbt_project.yml` vars, neither of which the
manifest carries.

## Inputs

| Input | Default | |
| :--- | :--- | :--- |
| `base-manifest` | — | required |
| `head-manifest` | — | required |
| `base-root` / `head-root` | inferred | improves resolution |
| `fail-on` | `breaking` | `breaking` · `possibly-breaking` · `never` |
| `fail-on-findings` | `true` | set `false` when a later job posts the result |
| `version` | pinned | whatbreaks version to install |
| `comment` | `false` | post directly; needs `pull-requests: write` |

Outputs: `failed`, `markdown-file`, `json-file`.

## Exit codes

| | |
| :--- | :--- |
| `0` | clean |
| `1` | findings at or above the threshold |
| `2` | **bad input** — nothing was analysed |

The action treats `2` as an error rather than as "nothing breaks", because a run
that analysed nothing has not established that anything is safe.

## Is it actually tested?

Yes. [`action-selftest.yml`](../.github/workflows/action-selftest.yml) runs the
real action against the committed [quickstart example](../examples/quickstart/)
on every change and asserts both directions: that it detects the known breaking
change, and that comparing the base against itself reports nothing.

An action nobody has executed is not a shipped feature.
