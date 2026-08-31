# Contributing to Repo Agent

Repo Agent is an early-stage local coding agent. Keep changes small,
evidence-backed, and compatible with the lightweight dependency set.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m compileall -q src tests
```

Changes to agent control flow, command execution, completion policy, provider
requests, or trajectory schemas should include focused tests. Never commit API
keys, private trajectories, generated workspaces, or credentials.

## Pull requests

- Explain the user-visible behavior and safety impact.
- Add or update tests and documentation.
- Preserve compatibility with unfinished legacy trajectories when practical.
- Keep provider-specific behavior inside the model adapter layer.
- Do not weaken destructive-command checks without a documented alternative.
