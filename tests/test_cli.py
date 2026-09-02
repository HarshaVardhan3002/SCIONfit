from scionarena.conformance.cli import main


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


def test_the_capability_report_precedes_the_run(capsys):
    """A user reads DECLARED_ABSENT before spending the run, not after it."""
    assert main(["check", "minrtt", "--no-colour", "--repeats", "1"]) == 0
    out = capsys.readouterr().out
    assert out.index("DECLARED_ABSENT") < out.index("R1"), "declaration first, probes after"


def test_the_capability_report_can_be_turned_off(capsys):
    assert main(["check", "minrtt", "--no-colour", "--repeats", "1", "--no-capabilities"]) == 0
    assert "gated probes will run" not in capsys.readouterr().out


def test_an_unloadable_model_exits_with_a_sentence(capsys):
    import pytest

    with pytest.raises(SystemExit) as raised:
        main(["check", "not.a.module:Model"])
    assert "pip install" in str(raised.value)


def test_the_umbrella_loads_a_model_without_running_anything(capsys):
    from scionarena.cli import main as umbrella

    assert umbrella(["models", "minrtt"]) == 0
    out = capsys.readouterr().out
    assert "MinRTTGreedy" in out and "R5" in out

    assert umbrella(["models", "nope.nope:X"]) == 2, "a bad spec is an exit code, not a traceback"


def test_the_umbrella_lists_the_builtin_specs(capsys):
    from scionarena.cli import main as umbrella

    assert umbrella(["models"]) == 0
    assert "scionarena.reference.models:MinRTTGreedy" in capsys.readouterr().out
