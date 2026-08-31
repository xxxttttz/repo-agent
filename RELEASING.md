# Releasing Repo Agent

## One-time repository setup

- Replace `Repo Agent contributors` in `pyproject.toml` and `LICENSE.md` with the intended author or organization.
- Add `[project.urls]` entries for the repository, documentation, and issue tracker.
- Confirm that the public repository name and package name are available.
- Initialize or clone a real Git repository before the first release and confirm its working tree is clean.
- Review `THIRD_PARTY_NOTICES.md` and preserve all applicable upstream notices.

## Release checklist

1. Confirm no API keys, credentials, trajectories, or private workspaces are included.
2. Run `python -m pytest -q` and `python -m compileall -q src tests`.
3. Build both distributions with `python -m build` in a release environment.
4. Inspect the wheel and source archive contents.
5. Update `CHANGELOG.md` and the version in `pyproject.toml` and `repo_agent.__version__` together.
6. Commit the release, create a signed `vX.Y.Z` tag, and publish a GitHub release.
7. Publish to a package index only after verifying the project name and metadata.
