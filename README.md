# ghosttrap-cli

Watch for errors streaming from ghosttrap.io in real time.

## Install

```
pip install ghosttrap-cli
```

Requires the [GitHub CLI](https://cli.github.com) (`gh`) for authentication.

## Use

```
ghosttrap watch
```

Authenticates via your local `gh` session, subscribes to error streams
for all repos you have access to, and prints events as they arrive.

Human-readable summaries go to stderr. Machine-readable JSON goes to
stdout, for piping into other tools.
