"""
Core smoke tests — no Claude, no git worktrees, no subprocesses.
Covers: Blackboard, _build_prompt, _parse_output, load_config.
"""
import json
import os
import tempfile

from blackboard import Blackboard
from models import Signal


# ── Blackboard ────────────────────────────────────────────────────────────────

def test_blackboard_write_and_read():
    with tempfile.TemporaryDirectory() as tmp:
        b = Blackboard(tmp)
        sig = Signal(type="TASK_DEFINED", payload={"task": "hello"}, from_role="orch")
        b.write(sig)
        all_ = b.get_all_signals()
        assert len(all_) == 1
        assert all_[0]["type"] == "TASK_DEFINED"


def test_blackboard_claim_and_done():
    with tempfile.TemporaryDirectory() as tmp:
        b = Blackboard(tmp)
        sig = Signal(type="TASK_DEFINED", payload={}, from_role="orch")
        b.write(sig)

        claimed = b.claim_next(["TASK_DEFINED"], "agent-1")
        assert claimed is not None
        assert claimed.status == "claimed"

        # Second claim should return None (already claimed)
        claimed2 = b.claim_next(["TASK_DEFINED"], "agent-2")
        assert claimed2 is None

        b.mark_done(claimed.id)
        all_ = b.get_all_signals()
        assert all_[0]["status"] == "done"


def test_blackboard_unclaim():
    with tempfile.TemporaryDirectory() as tmp:
        b = Blackboard(tmp)
        sig = Signal(type="FOO", payload={}, from_role="x")
        b.write(sig)

        claimed = b.claim_next(["FOO"], "a1")
        assert claimed is not None
        b.unclaim(claimed.id)

        # Should be claimable again
        claimed2 = b.claim_next(["FOO"], "a2")
        assert claimed2 is not None


def test_blackboard_has_signal():
    with tempfile.TemporaryDirectory() as tmp:
        b = Blackboard(tmp)
        assert not b.has_signal_of_type("DONE")
        b.write(Signal(type="DONE", payload={}, from_role="r"))
        assert b.has_signal_of_type("DONE")


def test_blackboard_get_last():
    with tempfile.TemporaryDirectory() as tmp:
        b = Blackboard(tmp)
        b.write(Signal(type="DONE", payload={"content": "first"}, from_role="r"))
        b.write(Signal(type="DONE", payload={"content": "second"}, from_role="r"))
        last = b.get_last("DONE")
        assert last is not None
        assert last["payload"]["content"] == "second"


def test_blackboard_claim_fifo():
    with tempfile.TemporaryDirectory() as tmp:
        b = Blackboard(tmp)
        s1 = Signal(type="T", payload={"n": 1}, from_role="r")
        s2 = Signal(type="T", payload={"n": 2}, from_role="r")
        b.write(s1)
        b.write(s2)
        c = b.claim_next(["T"], "a")
        assert c.id == s1.id  # FIFO


# ── _build_prompt ─────────────────────────────────────────────────────────────

def test_build_prompt_no_format_error():
    """Prompts contain JSON examples with {signal}/{content} — must NOT crash."""
    from agent import _build_prompt

    role_config = {
        "prompt": (
            'Do the task: {task}\n'
            'Signal payload: {signal_payload}\n'
            'Context: {context}\n'
            'Output: {"signal": "CODE_READY", "content": "done"}'
        )
    }
    sig = Signal(type="TASK_DEFINED", payload={"task": "fix bug"}, from_role="orch")
    result = _build_prompt("fix bug", role_config, sig, [])

    assert "fix bug" in result
    assert '{"signal": "CODE_READY", "content": "done"}' in result


def test_build_prompt_substitution():
    from agent import _build_prompt

    role_config = {"prompt": "task={task} payload={signal_payload} ctx={context}"}
    sig = Signal(type="T", payload={"x": 1}, from_role="r")
    result = _build_prompt("mytask", role_config, sig, [{"a": "b"}])

    assert "task=mytask" in result
    assert '"x": 1' in result
    assert '"a": "b"' in result


# ── _parse_output ─────────────────────────────────────────────────────────────

def test_parse_output_json_block():
    from agent import _parse_output

    out = 'some text\n```json\n{"signal": "DONE", "content": "ok"}\n```'
    parsed = _parse_output(out)
    assert isinstance(parsed, dict)
    assert parsed["signal"] == "DONE"


def test_parse_output_list_block():
    from agent import _parse_output

    out = '```json\n[{"signal": "S1", "content": "a"}, {"signal": "S2", "content": "b"}]\n```'
    parsed = _parse_output(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 2


def test_parse_output_inline_json():
    from agent import _parse_output

    out = 'thinking...\n{"signal": "DONE", "content": "finished"}'
    parsed = _parse_output(out)
    assert isinstance(parsed, dict)
    assert parsed["signal"] == "DONE"


def test_parse_output_none():
    from agent import _parse_output

    assert _parse_output("no json here at all") is None


# ── load_config ───────────────────────────────────────────────────────────────

def test_load_config_swarm():
    from swarm import load_config
    from pathlib import Path

    cfg_path = str(Path(__file__).parent / "roles" / "swarm.yaml")
    cfg = load_config(cfg_path)
    assert "roles" in cfg
    names = [r["name"] for r in cfg["roles"]]
    assert "coder" in names
    assert "selector" in names
    assert "reviewer" in names
    # fan_out and no_propagate injected via override
    coder = next(r for r in cfg["roles"] if r["name"] == "coder")
    assert coder.get("fan_out") is True
    assert coder.get("no_propagate") is True


def test_load_config_cooperative():
    from swarm import load_config
    from pathlib import Path

    cfg_path = str(Path(__file__).parent / "roles" / "cooperative.yaml")
    cfg = load_config(cfg_path)
    names = [r["name"] for r in cfg["roles"]]
    assert "decomposer" in names
    assert "developer" in names
    assert "integrator" in names
    assert "reviewer" in names


# ── runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    exit(failed)
