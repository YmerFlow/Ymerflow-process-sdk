"""Synthesize a Dockerfile for a YmerFlow runner environment from a small parameter set.

The single, dependency-free (stdlib-only) text function shared by both image-building code paths:

* ``docker/build.sh`` (host Docker daemon) builds the default base-runner image, and
* the ``create_environment`` process type (in-pod kaniko) builds custom environments.

Both call ``params_to_dockerfile`` with the same knobs, so the produced Dockerfiles never drift.
The build engine and the environment-registration step genuinely differ between the two and stay
in their respective callers; only the Dockerfile text is shared here.
"""

# Kaniko has no standalone executor binary release — the only way to obtain it is to copy it out of
# the official kaniko image via a multi-stage build (see ``install_kaniko`` below).
KANIKO_IMAGE = "gcr.io/kaniko-project/executor:v1.24.0"


def params_to_dockerfile(base_image, python_packages="", dockerfile_instructions="",
                         install_kaniko=False):
    """Return the text of a Dockerfile for a YmerFlow runner environment.

    Parameters
    ----------
    base_image : str
        The ``FROM`` image (e.g. ``python:3.11-slim-trixie``).
    python_packages : str
        requirements.txt-format text — one package per line; blank lines and ``#`` comments are
        ignored. git URLs are allowed, e.g. ``git+https://github.com/org/repo.git`` or
        ``pkg[extra] @ git+https://github.com/org/repo.git``.
    dockerfile_instructions : str
        Extra Dockerfile lines emitted *before* the pip install — this is where system-level setup
        belongs (apt build toolchain, Node.js, compiler env), because it must be in place before
        pip compiles anything (e.g. pyinterp against Boost).
    install_kaniko : bool
        Emit the multi-stage ``FROM …/executor AS kaniko`` prelude plus
        ``COPY --from=kaniko /kaniko/executor /kaniko-executor`` so the built image can itself build
        images (``create_environment`` shells out to ``/kaniko-executor``).

    Ordering: optional kaniko prelude → ``FROM base_image`` → ``WORKDIR /app`` → optional kaniko
    copy → ``dockerfile_instructions`` → pip install → the runner's own schema bake + ENTRYPOINT.
    The trailing ``python -m ymerflow_runner.get_schema`` (bakes ``/app/process_schemas.json`` for
    extraction) and ``ENTRYPOINT ["python", "-u", "-m", "ymerflow_runner"]`` are appended here
    because they are this SDK's own harness — they version as a unit with this function, and every
    runner environment needs them. This requires ``ymerflow-process-sdk`` to be among
    ``python_packages`` (it is, in the default base-runner param set).
    """
    lines = []

    if install_kaniko:
        lines.append(f"FROM {KANIKO_IMAGE} AS kaniko")
        lines.append("")

    lines.append(f"FROM {base_image}")
    lines.append("WORKDIR /app")
    if install_kaniko:
        lines.append("COPY --from=kaniko /kaniko/executor /kaniko-executor")
    lines.append("")

    instructions = (dockerfile_instructions or "").strip()
    if instructions:
        lines.append(instructions)
        lines.append("")

    packages = [ln.strip() for ln in (python_packages or "").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    if packages:
        lines.append("RUN pip install --no-cache-dir \\")
        for i, pkg in enumerate(packages):
            # Each requirement is double-quoted so PEP 508 direct references with extras and
            # spaces — e.g. `aem-processes[all] @ git+https://…` — survive the shell unbroken.
            sep = " \\" if i < len(packages) - 1 else ""
            lines.append(f'    "{pkg}"{sep}')
        lines.append("")

    lines.append("RUN python -m ymerflow_runner.get_schema")
    lines.append('ENTRYPOINT ["python", "-u", "-m", "ymerflow_runner"]')
    lines.append("")

    return "\n".join(lines)
