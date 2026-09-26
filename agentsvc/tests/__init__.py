"""Test package for `agentsvc`.

Run with (from the repository root):

    cd agentsvc && python3.12 -m unittest discover -s tests -t . -v

`-t .` sets the top-level directory to `agentsvc/`, so `import agent_svc` (the
package under `agentsvc/agent_svc/`) works without installing anything, and
each test module adds `agentsvc/libexec` to `sys.path` itself before
importing `codex_child` / `image_state` (plain scripts, not a package) or the
`fake_codex.py` helper in this directory. No sudo, docker, or real `codex`
binary is required: tests that need the child process run it directly with
the current Python interpreter (`command_prefix=[]`, `python_bin=sys.executable`)
and set `AGENT_CHILD_TEST_MODE=1` plus `AGENT_CHILD_TEST_*` overrides to point
it at a scratch work root and the `fake_codex.py` stand-in for `codex`.
"""
