# Security Policy

## Supported versions

Repo Agent is currently alpha software. Security fixes are applied to the
latest `0.1.x` release line only.

## Execution model

Repo Agent executes model-generated shell commands in the configured local
workspace. Its timeout, output limits, evidence policy, and destructive-command
checks are application-level safeguards, not an operating-system sandbox.
Use a container, namespace, virtual machine, or disposable account when working
with untrusted repositories or models.

Do not place secrets in task text, trajectory files, model configuration, or a
workspace the model can read. Environment variable names are supported for API
authentication, but commands executed by the agent inherit the local process
environment.

## Reporting a vulnerability

Once the public repository is created, report vulnerabilities privately using
its GitHub Security Advisory interface. Until then, contact the project owner
through a private channel. Do not open a public issue containing exploit details,
credentials, or private trajectories.
