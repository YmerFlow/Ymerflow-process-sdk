# Ymerflow-process-sdk

The **process/runner** side of YmerFlow (parallel to `Ymerflow-plugin-sdk`, which is for plugin
authoring). It contains:

- **`ymerflow_runner`** — the runner harness, an importable package:
  - `python -m ymerflow_runner` — the image `ENTRYPOINT`; loads the requested process type from the
    `ymerflow.process_types` entry-point group and runs it.
  - `python -m ymerflow_runner.get_schema` — the build-time schema bake; writes
    `/app/process_schemas.json` from all installed process types.
  - `storage_credentials_client` / `storage_credential_refresher` — short-lived storage-credential
    refresh loop for long-running jobs.
  - `xyz_utils` — XYZ helper utilities.
- **`params_to_dockerfile(base_image, python_packages, dockerfile_instructions, install_kaniko=False)`**
  — the single, stdlib-only Dockerfile-synthesis function shared by `docker/build.sh` (host build of
  the default base runner) and the `create_environment` process type (in-pod kaniko build of custom
  environments), so the two never drift.
- **`create_environment`** — the process type that builds+registers custom environments.

Installed via `pip install "git+https://github.com/YmerFlow/Ymerflow-process-sdk.git"`.
