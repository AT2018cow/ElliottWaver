import json

from typer.testing import CliRunner

from elliottwaver import __version__
from elliottwaver.cli.main import app


def test_version_exits_without_a_subcommand():
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"{__version__}\n"


def test_doctor_lists_all_supported_pattern_families():
    result = CliRunner().invoke(app, ["doctor", "--market", "CN"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["supported_patterns"] == [
        "IMPULSE",
        "ZIGZAG_ABC",
        "TRIANGLE",
        "FLAT",
        "DIAGONAL",
        "WXY",
    ]
