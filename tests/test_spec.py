from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from syncline.cli import app
from syncline.spec import SpecError, json_schema, load_flow

EXAMPLE = Path(__file__).parent.parent / "examples" / "acme" / "acme_users.yaml"


def write(tmp_path: Path, data: dict | str) -> Path:
    p = tmp_path / "flow.yaml"
    p.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return p


def base() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def problems(tmp_path: Path, data: dict | str) -> list[str]:
    with pytest.raises(SpecError) as e:
        load_flow(write(tmp_path, data))
    return e.value.problems


def test_example_flow_is_valid() -> None:
    spec = load_flow(EXAMPLE)
    assert spec.flow == "acme_users"
    assert spec.mapping["hire_date"].format == "%d/%m/%Y"
    assert spec.mapping["department"].values["ventes"] == "Sales"
    assert spec.load.deactivate is not None
    assert spec.load.deactivate.set == {"status": "inactive"}


def test_shorthand_mapping(tmp_path: Path) -> None:
    data = base()
    data["mapping"]["email"] = "E-mail"
    spec = load_flow(write(tmp_path, data))
    assert spec.mapping["email"].source == "E-mail"
    assert spec.mapping["email"].type == "string"


def test_unknown_key_is_a_typo(tmp_path: Path) -> None:
    data = base()
    data["mapping"]["email"]["transfrom"] = ["trim"]
    assert any("mapping.email.transfrom: unknown key" in p for p in problems(tmp_path, data))


def test_contract_fields_must_be_mapped(tmp_path: Path) -> None:
    data = base()
    data["contract"]["required"].append("phone")
    assert any("contract.required: phone not in mapping" in p for p in problems(tmp_path, data))


def test_key_must_be_required(tmp_path: Path) -> None:
    data = base()
    data["load"]["key"] = ["department"]
    assert any("must be in contract.required: department" in p for p in problems(tmp_path, data))


def test_format_only_for_dates(tmp_path: Path) -> None:
    data = base()
    data["mapping"]["email"]["format"] = "%d/%m/%Y"
    assert any("only applies to fields of type 'date'" in p for p in problems(tmp_path, data))


def test_field_needs_source_or_default(tmp_path: Path) -> None:
    data = base()
    data["mapping"]["country"] = {"type": "string"}
    assert any("needs 'from'" in p for p in problems(tmp_path, data))


def test_full_snapshot_needs_deactivate(tmp_path: Path) -> None:
    data = base()
    del data["load"]["deactivate"]
    assert any("needs 'deactivate'" in p for p in problems(tmp_path, data))


def test_incremental_rejects_deactivate(tmp_path: Path) -> None:
    data = base()
    data["load"]["mode"] = "incremental"
    assert any("only applies to mode 'full_snapshot'" in p for p in problems(tmp_path, data))


def test_bad_flow_name(tmp_path: Path) -> None:
    data = base()
    data["flow"] = "Acme Users"
    assert any(p.startswith("flow:") for p in problems(tmp_path, data))


def test_yaml_error_has_line_number(tmp_path: Path) -> None:
    assert any("line 2" in p for p in problems(tmp_path, "flow: x\n  bad: [indent\n"))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="file not found"):
        load_flow(tmp_path / "nope.yaml")


def test_schema_uses_from_alias() -> None:
    schema = json_schema()
    assert "from" in schema["$defs"]["FieldMapping"]["properties"]


def test_cli_validate_ok() -> None:
    r = CliRunner().invoke(app, ["validate", str(EXAMPLE)])
    assert r.exit_code == 0, r.output
    assert "is valid" in " ".join(r.output.split())


def test_cli_validate_error(tmp_path: Path) -> None:
    data = base()
    data["load"]["key"] = ["nope"]
    r = CliRunner().invoke(app, ["validate", str(write(tmp_path, data))])
    assert r.exit_code == 1


def test_source_type_defaults_to_file(tmp_path: Path) -> None:
    data = base()
    del data["source"]["type"]
    assert load_flow(write(tmp_path, data)).source.type == "file"


def test_remote_sources_are_valid(tmp_path: Path) -> None:
    data = base()
    data["source"] = {"type": "s3", "bucket": "acme-hr", "path": "exports/employes_*.csv"}
    assert load_flow(write(tmp_path, data)).source.type == "s3"
    data["source"] = {
        "type": "sftp", "host": "sftp.acme.example", "username": "acme",
        "password": "env:ACME_SFTP_PASSWORD", "path": "/outgoing/employes_*.csv",
    }  # fmt: skip
    spec = load_flow(write(tmp_path, data))
    assert spec.source.type == "sftp"
    assert spec.source.port == 22 and spec.source.min_age_seconds == 60


def test_a_password_in_the_flow_file_is_refused(tmp_path: Path) -> None:
    data = base()
    data["source"] = {
        "type": "sftp", "host": "h", "username": "u", "password": "hunter2", "path": "/a/*.csv"
    }  # fmt: skip
    (p,) = problems(tmp_path, data)
    assert p.startswith("source.password: secrets don't go in flow files")


@pytest.mark.parametrize(
    ("path", "message"),
    [("exports/", "must end with a file name"), ("exp*/a.csv", "only the file name may be")],
)
def test_remote_path_pattern_only_in_the_file_name(tmp_path: Path, path: str, message: str) -> None:
    data = base()
    data["source"] = {"type": "s3", "bucket": "acme-hr", "path": path}
    assert message in " ".join(problems(tmp_path, data))


def test_unknown_source_type(tmp_path: Path) -> None:
    data = base()
    data["source"] = {"type": "ftp", "path": "x.csv"}
    assert problems(tmp_path, data) == ["source: unknown type 'ftp'; use one of: file, s3, sftp"]
