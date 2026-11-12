# engine

The distillation engine behind 视频蒸馏专家. This directory is a **verbatim copy**
of the `v2n` package from the upstream project, so that this repository is
self-contained and a deployment never needs credentials to a private repository.

- Upstream: `https://github.com/Current-off/video2notes`
- Vendored commit: recorded in `MANIFEST.json`
- Per-file hashes: `MANIFEST.json`, verifiable with `python scripts/manifest.py verify`

Do not edit files here by hand. Change the upstream project, then re-sync with
`python scripts/manifest.py sync --from <path-to-upstream-checkout>`; otherwise
the two copies drift silently and the hashes stop meaning anything.

Install it directly if you want the CLI without the deployment wrapper:

```bash
python -m pip install -e .
```
