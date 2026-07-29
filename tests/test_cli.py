from scionfit.cli import main


def test_list(capsys):
    assert main(["list"]) == 0
    assert "reference" in capsys.readouterr().out


def test_check_reference(capsys):
    assert main(["check", "reference", "--no-colour", "--repeats", "1"]) == 0
    assert "CONFORMANT" in capsys.readouterr().out


def test_strict_fails_on_nonconformant():
    assert main(["check", "ema", "--no-colour", "--repeats", "1", "--strict"]) == 1


def test_json_format(capsys):
    import json
    assert main(["check", "reference", "--format", "json", "--repeats", "1"]) == 0
    json.loads(capsys.readouterr().out)
