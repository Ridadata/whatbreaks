from __future__ import annotations

from pathlib import Path

from tests.conftest import manifest_payload, model_node
from whatbreaks.manifest import load_manifest
from whatbreaks.project import read_project_vars
from whatbreaks.sql import SqlRecovery


def write_project(tmp_path: Path, body: str) -> Path:
    (tmp_path / "dbt_project.yml").write_text(body, encoding="utf-8")
    return tmp_path


def test_missing_project_file_is_not_an_error(tmp_path: Path) -> None:
    assert read_project_vars(tmp_path) == {}
    assert read_project_vars(None) == {}


def test_malformed_yaml_degrades_instead_of_raising(tmp_path: Path) -> None:
    """A broken project file must not abort analysis the manifest can still support."""
    root = write_project(tmp_path, "vars: [unclosed\n  - :::")
    assert read_project_vars(root) == {}


def test_flat_vars_are_read(tmp_path: Path) -> None:
    root = write_project(tmp_path, "name: p\nvars:\n  start_date: '2020-01-01'\n  n: 5\n")
    assert read_project_vars(root) == {"start_date": "2020-01-01", "n": 5}


def test_package_scoped_vars_are_exposed_by_name(tmp_path: Path) -> None:
    root = write_project(
        tmp_path,
        "name: p\nvars:\n  global_var: 1\n  some_package:\n    inner_var: 2\n",
    )
    result = read_project_vars(root)
    assert result["global_var"] == 1
    assert result["inner_var"] == 2
    assert result["some_package"] == {"inner_var": 2}


def test_no_vars_block(tmp_path: Path) -> None:
    assert read_project_vars(write_project(tmp_path, "name: p\nversion: '1.0'\n")) == {}


def test_var_resolves_from_dbt_project_yml(write_manifest, tmp_path: Path) -> None:
    """The whole point: real values, offline, rather than a guess or a failure."""
    path = write_manifest(
        manifest_payload(
            nodes={
                "model.testproj.a": model_node(
                    "a", raw_code="select * from t where d > '{{ var('start_date') }}'"
                )
            }
        )
    )
    write_project(path.parent, "name: testproj\nvars:\n  start_date: '2021-06-01'\n")
    recovery = SqlRecovery(load_manifest(path), project_root=path.parent)
    sql = recovery.recover(recovery.manifest.models["model.testproj.a"]).sql or ""
    assert "2021-06-01" in sql


def test_project_var_wins_over_caller_default(write_manifest, tmp_path: Path) -> None:
    path = write_manifest(
        manifest_payload(
            nodes={"model.testproj.a": model_node("a", raw_code="select {{ var('n', 999) }} as n")}
        )
    )
    write_project(path.parent, "name: testproj\nvars:\n  n: 7\n")
    recovery = SqlRecovery(load_manifest(path), project_root=path.parent)
    sql = recovery.recover(recovery.manifest.models["model.testproj.a"]).sql or ""
    assert " 7 " in sql
    assert "999" not in sql


def test_config_get_reads_node_config(write_manifest) -> None:
    """`config.get(...)` is real dbt API; modelling config as a bare no-op
    function makes it resolve to an undefined named `get`."""
    path = write_manifest(
        manifest_payload(
            nodes={
                "model.testproj.a": model_node(
                    "a",
                    raw_code="select 1 as x -- {{ config.get('unique_key') }}",
                    unique_key="id",
                    materialized="incremental",
                )
            }
        )
    )
    recovery = SqlRecovery(load_manifest(path))
    sql = recovery.recover(recovery.manifest.models["model.testproj.a"]).sql or ""
    assert "id" in sql


def test_config_get_returns_default_when_absent(write_manifest) -> None:
    path = write_manifest(
        manifest_payload(
            nodes={
                "model.testproj.a": model_node(
                    "a", raw_code="select 1 as x -- {{ config.get('nope', 'fallback') }}"
                )
            }
        )
    )
    recovery = SqlRecovery(load_manifest(path))
    sql = recovery.recover(recovery.manifest.models["model.testproj.a"]).sql or ""
    assert "fallback" in sql


def test_config_call_still_renders_nothing(write_manifest) -> None:
    path = write_manifest(
        manifest_payload(
            nodes={
                "model.testproj.a": model_node(
                    "a", raw_code="{{ config(materialized='view') }}select 1 as x"
                )
            }
        )
    )
    recovery = SqlRecovery(load_manifest(path))
    sql = recovery.recover(recovery.manifest.models["model.testproj.a"]).sql or ""
    assert sql.strip().startswith("select")


def test_dbt_return_is_available_to_macros(write_manifest) -> None:
    """`{{ return(x) }}` is used pervasively; omitting it cost 96/164 models."""
    path = write_manifest(
        manifest_payload(
            nodes={"model.testproj.a": model_node("a", raw_code="select {{ pick() }} as v")},
            macros={
                "macro.p.pick": {
                    "name": "pick",
                    "package_name": "p",
                    "macro_sql": "{% macro pick() %}{{ return(11) }}{% endmacro %}",
                }
            },
        )
    )
    recovery = SqlRecovery(load_manifest(path))
    assert "11" in (recovery.recover(recovery.manifest.models["model.testproj.a"]).sql or "")
