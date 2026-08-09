---
name: Wrong lineage or a wrong finding
about: whatbreaks reported something incorrect, or missed something
labels: bug
---

## What happened

<!-- Was a real break missed (false negative), or was something reported that
     should not have been (false positive)? False negatives are the more serious
     of the two: someone merges a breaking change believing it is safe. -->

## Minimal SQL

```sql
-- base
select ...

-- head
select ...
```

## What whatbreaks said

```
paste the output, including the coverage lines
```

## What you expected

<!-- Which severity, and why. -->

## Environment

- whatbreaks version:
- dbt version / adapter:

---

**Even more useful than this issue:** a case in
[`tests/false_positives/cases/`](../../tests/false_positives/README.md) if the
tool over-reported. One YAML file, no code, and it becomes the regression test.
