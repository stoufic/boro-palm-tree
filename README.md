# Blast Radius selector

A test-impact selector for the sqlglot Blast Radius challenge. The image builds
an exact pytest-context coverage map from the pinned sqlglot commit, then uses
changed lines, enclosing functions, imports, fixtures, and dialect ownership to
choose and schedule tests.

Build and score it from the repository root:

```sh
docker build --platform linux/amd64 -t blast-radius-entry .
make -C BlastRadius/blast-radius-sample sample IMAGE=blast-radius-entry
```
