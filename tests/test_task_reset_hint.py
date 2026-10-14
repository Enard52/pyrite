"""A refused attempt to release a held task names the command that does it (#52).

`task update -s open` on a task a worker holds is refused (releasing a claim
is a privileged recovery path), and `pyrite task reset` is the command that
does it. The hint goes in the refusal's `suggestion` (docs/json-contracts.md,
"Error shape"), so it reaches the CLI, MCP and REST the same way. `reset`
accepts only `in_progress` and `blocked`, so only those two get the hint: a
`claimed` task would be pointed at a command that refuses it.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from pyrite.cli import app
from pyrite.config import KBConfig, PyriteConfig, Settings
from pyrite.exceptions import ValidationError
from pyrite.server.mcp_server import _refusal
from pyrite.services.task_service import TaskService
from pyrite.storage.database import PyriteDB

runner = CliRunner()

PATHS = {
    "claimed": ["claimed"],
    "in_progress": ["claimed", "in_progress"],
    "blocked": ["claimed", "in_progress", "blocked"],
    "done": ["claimed", "in_progress", "done"],
}


@pytest.fixture
def env():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        kb_path = tmp / "tasks-kb"
        (kb_path / "tasks").mkdir(parents=True)
        config = PyriteConfig(
            knowledge_bases=[KBConfig(name="t", path=kb_path, kb_type="task")],
            settings=Settings(index_path=tmp / "index.db"),
        )
        db = PyriteDB(config.settings.index_path)
        db.register_kb(name="t", kb_type="task", path=str(kb_path))
        svc = TaskService(config, db)

        def task_in(status: str) -> str:
            task_id = svc.create_task(kb_name="t", title=f"held {status}")["entry_id"]
            for step in PATHS[status]:
                extra = {"assignee": "agent:a"} if step == "claimed" else {}
                svc.update_task(task_id, "t", status=step, **extra)
            return task_id

        with patch("pyrite.cli.context.load_config", return_value=config):
            yield {"svc": svc, "task_in": task_in}
        db.close()


def _cli_update_open(task_id: str):
    return runner.invoke(app, ["task", "update", task_id, "-k", "t", "-s", "open", "-f", "json"])


@pytest.mark.parametrize("status", ["in_progress", "blocked"])
def test_cli_refusal_names_reset(env, status):
    task_id = env["task_in"](status)
    result = _cli_update_open(task_id)
    assert result.exit_code != 0
    body = json.loads(result.stdout)  # stdout stays one JSON document
    assert "pyrite task reset" in body["suggestion"]


@pytest.mark.parametrize("status", ["in_progress", "blocked"])
def test_mcp_and_service_refusal_name_reset(env, status):
    task_id = env["task_in"](status)
    with pytest.raises(ValidationError) as caught:
        env["svc"].update_task(task_id, "t", status="open")
    assert "pyrite task reset" in caught.value.suggestion
    assert "pyrite task reset" in _refusal(caught.value)["suggestion"]


@pytest.mark.control(reason="the guard on the hint: reset itself is unchanged by this fix")
@pytest.mark.parametrize("status", ["in_progress", "blocked"])
def test_the_named_command_works_from_the_refused_state(env, status):
    """Any refusal that names a command must have that command succeed."""
    task_id = env["task_in"](status)
    assert _cli_update_open(task_id).exit_code != 0
    result = runner.invoke(app, ["task", "reset", task_id, "-k", "t", "-f", "json"])
    assert result.exit_code == 0, result.stdout
    assert env["svc"].get_task(task_id, "t", readable_kbs=None)["status"] == "open"


@pytest.mark.control(reason="reset refuses a claimed task, so the refusal must not name it")
def test_claimed_refusal_does_not_name_reset(env):
    result = _cli_update_open(env["task_in"]("claimed"))
    assert result.exit_code != 0
    assert "reset" not in json.dumps(json.loads(result.stdout))


@pytest.mark.control(reason="a finished task is not a held claim; reset would refuse it")
def test_done_refusal_does_not_name_reset(env):
    result = _cli_update_open(env["task_in"]("done"))
    assert result.exit_code != 0
    assert "reset" not in json.dumps(json.loads(result.stdout))


def test_rich_refusal_shows_the_hint_too(env):
    task_id = env["task_in"]("in_progress")
    result = runner.invoke(
        app, ["task", "update", task_id, "-k", "t", "-s", "open", "--format", "rich"]
    )
    assert result.exit_code != 0
    assert "pyrite task reset" in result.stdout + result.stderr
