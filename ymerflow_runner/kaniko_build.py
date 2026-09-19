"""Run the kaniko executor **isolated in a chroot** so it can't corrupt the runner pod.

Kaniko is designed to *own* the filesystem of the container it runs in: it extracts the base image
over ``/``, runs the Dockerfile's ``RUN`` steps against the real filesystem, and snapshots the
diffs. When ``create_environment`` shelled out to ``/kaniko-executor`` directly, that meant every
environment build replaced the *live runner's* rootfs — including its
``/usr/local/lib/python3.11/site-packages`` — with the freshly built image. It broke in production
when a kaniko ``pip install`` (fsspec/s3fs unpinned) overwrote the runner's own fsspec with a newer
version, leaving a half-cached / half-fresh module set that crashed the post-build ``fsspec.open()``
(see docs/plans/fix-kaniko-clobbers-runner-filesystem.md). Older code fought this with a pile of
``--ignore-path`` survival hacks to keep the crane binary + a schema-extraction workspace alive
across the wipe; the chroot removes the need for all of them.

:func:`kaniko_build` runs kaniko in a child process that ``chroot()``s into a prepared throwaway
rootfs first. Kaniko extracts the base image into the *chroot's* ``/``, runs ``pip install`` there,
snapshots, and pushes to the registry — all inside the chroot. The parent runner process keeps the
pod's real ``/`` pristine, so anything the caller does afterward (``crane export``,
``fsspec.open()``) runs against an unmodified ``site-packages``. Same single billed pod, no new
RBAC, no extra pods.

Why it works with the capabilities we already have: the Job pod sets no ``securityContext``, so it
runs as root with the default container capability set — ``CAP_SYS_CHROOT`` (chroot), ``CAP_MKNOD``
+ ``CAP_CHOWN``/``CAP_DAC_OVERRIDE``/``CAP_FOWNER`` (device nodes + kaniko's image extraction).
``CAP_SYS_ADMIN`` is *not* default, so we cannot ``mount`` — the chroot avoids needing real mounts
by copying everything in (certs, resolv.conf, docker config) and faking the one ``/proc`` file
kaniko reads (below). Validated only for a baseline/privileged namespace; a PodSecurity-``restricted``
namespace would already forbid the current root-writing-to-``/`` kaniko.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile


# The base-runner image copies the kaniko binary here (see params_to_dockerfile.install_kaniko:
# ``COPY --from=kaniko /kaniko/executor /kaniko-executor``).
DEFAULT_EXECUTOR_PATH = "/kaniko-executor"

# Character device nodes kaniko / pip / shells in RUN steps expect. Python 3.6+ uses getrandom()
# rather than /dev/urandom, but tooling in RUN steps still expects /dev/null etc.
_DEV_NODES = {
    "null": (1, 3),
    "zero": (1, 5),
    "random": (1, 8),
    "urandom": (1, 9),
}

# Kaniko reads /proc/self/mountinfo to build its snapshot ignore-list and errors immediately if it
# is absent — but a static single-line fake is enough (a real /proc would need a mount we can't do
# without CAP_SYS_ADMIN). Validated with kaniko v1.24.0.
_FAKE_MOUNTINFO = "1 0 0:1 / / rw,relatime - overlay overlay rw\n"


def kaniko_build(dockerfile_text, destination, *, registry_url=None, registry_auth=None,
                 extra_args=(), executor_path=DEFAULT_EXECUTOR_PATH):
    """Build and push ``destination`` from ``dockerfile_text`` with kaniko, isolated in a chroot.

    Parameters
    ----------
    dockerfile_text : str
        The full Dockerfile text (as produced by :func:`params_to_dockerfile`).
    destination : str
        The image reference kaniko pushes to (``--destination``). Callers that must dodge the
        go-containerregistry ``:443`` auth-stripping bug should pass the *bare-host* form here.
    registry_url : str or None
        Registry host used as the key in the docker ``config.json`` ``auths`` map. Must match the
        host in ``destination`` (bare host, if that is what you push to). Required when
        ``registry_auth`` is given.
    registry_auth : str or None
        Base64 ``user:pass`` auth string for ``registry_url``. When given, a docker ``config.json``
        is staged inside the chroot at the path kaniko reads.
    extra_args : sequence of str
        Extra kaniko flags (e.g. ``--insecure``, ``--skip-tls-verify-registry=…``).
    executor_path : str
        Path to the kaniko binary in the *outer* image (default ``/kaniko-executor``).

    Notes
    -----
    Streams kaniko's output straight to this process's stdout/stderr (no capture) so build progress
    is visible live in the pod log stream, and imposes **no** inner timeout — the only bound on a
    build is the Job's ``activeDeadlineSeconds``.

    Raises
    ------
    RuntimeError
        If kaniko exits non-zero.
    """
    if not os.path.exists(executor_path):
        raise RuntimeError(
            f"kaniko executor not found at {executor_path!r} — this environment was not built with "
            f"install_kaniko=true, so it cannot build images."
        )

    root = tempfile.mkdtemp(prefix="kaniko-chroot-")
    try:
        # 1. The kaniko executor + the /kaniko workdir dance. Kaniko reserves its working dir and
        #    excludes it from every build stage — so a Dockerfile with
        #    `COPY --from=kaniko /kaniko/executor /kaniko-executor` (any install_kaniko build) would
        #    find /kaniko/executor invisible. Relocating kaniko's workdir to /kaniko-parent (via
        #    KANIKO_DIR, set on the child env below) leaves /kaniko an ordinary readable path.
        #    Kaniko relocates by copying an existing /kaniko dir to KANIKO_DIR at startup, so /kaniko
        #    must exist first. Mirror the pod layout the workaround expects, but inside the chroot.
        shutil.copy(executor_path, os.path.join(root, "kaniko-executor"))
        os.chmod(os.path.join(root, "kaniko-executor"), 0o755)
        docker_cfg_dir = os.path.join(root, "kaniko", ".docker")
        os.makedirs(docker_cfg_dir, exist_ok=True)

        # 2. CA certs + resolv.conf + hosts so kaniko can pull/push over TLS, resolve the registry
        #    and base-image hosts via DNS, and resolve local names (e.g. localhost). copytree
        #    dereferences symlinks (default) so the chroot is self-contained.
        os.makedirs(os.path.join(root, "etc", "ssl"), exist_ok=True)
        if os.path.isdir("/etc/ssl/certs"):
            shutil.copytree("/etc/ssl/certs", os.path.join(root, "etc", "ssl", "certs"))
        for etc_file in ("resolv.conf", "hosts"):
            if os.path.exists(f"/etc/{etc_file}"):
                shutil.copy(f"/etc/{etc_file}", os.path.join(root, "etc", etc_file))

        # 3. Registry auth. With KANIKO_DIR set, kaniko resets DOCKER_CONFIG to <KANIKO_DIR>/.docker
        #    at startup and reaches it by copying /kaniko -> /kaniko-parent — so the config must sit
        #    at /kaniko/.docker to ride that copy to /kaniko-parent/.docker, exactly where kaniko then
        #    looks. Key the auth to the registry host we actually push to (see `registry_url`).
        if registry_auth:
            if not registry_url:
                raise ValueError("registry_url is required when registry_auth is given")
            config = {"auths": {registry_url: {"auth": registry_auth}}}
            with open(os.path.join(docker_cfg_dir, "config.json"), "w") as f:
                json.dump(config, f)

        # 4. Build context + Dockerfile inside the chroot.
        context_dir = os.path.join(root, "context")
        os.makedirs(context_dir, exist_ok=True)
        with open(os.path.join(context_dir, "Dockerfile"), "w") as f:
            f.write(dockerfile_text)

        # 5. Device nodes via mknod (needs CAP_MKNOD, in the default cap set).
        dev_dir = os.path.join(root, "dev")
        os.makedirs(dev_dir, exist_ok=True)
        for name, (major, minor) in _DEV_NODES.items():
            os.mknod(
                os.path.join(dev_dir, name),
                stat.S_IFCHR | 0o666,
                os.makedev(major, minor),
            )

        # 6. Static /proc/self/mountinfo (a real /proc needs a mount we can't do).
        proc_self = os.path.join(root, "proc", "self")
        os.makedirs(proc_self, exist_ok=True)
        with open(os.path.join(proc_self, "mountinfo"), "w") as f:
            f.write(_FAKE_MOUNTINFO)

        env = dict(os.environ, KANIKO_DIR="/kaniko-parent")

        cmd = [
            "chroot", root, "/kaniko-executor",
            # Inside a chroot kaniko's "am I in a container?" heuristic fails and it refuses without
            # --force; --force is correct/safe here precisely because we've contained it in the chroot.
            "--force",
            # Exclude our staging scaffolding from the snapshot so it doesn't bloat/pollute layers.
            # NB: deliberately do NOT ignore /kaniko — an install_kaniko build's Dockerfile does
            # `COPY --from=kaniko /kaniko/executor …`, and --ignore-path=/kaniko would make kaniko
            # skip that cross-stage source ("Not adding /kaniko/executor because it is ignored") so
            # the copy fails. /kaniko needs no ignoring anyway: with KANIKO_DIR=/kaniko-parent, kaniko
            # relocates and deletes /kaniko at startup (before any snapshot), and auto-ignores its
            # /kaniko-parent workdir.
            "--ignore-path=/proc",
            "--ignore-path=/dev",
            "--ignore-path=/context",
            "--context=dir:///context",
            "--dockerfile=/context/Dockerfile",
            f"--destination={destination}",
            *extra_args,
        ]

        # Stream to the pod's stdout/stderr (no capture); no inner timeout — the Job's
        # activeDeadlineSeconds is the only build deadline.
        result = subprocess.run(cmd, env=env)
        if result.returncode != 0:
            raise RuntimeError(f"Kaniko build failed with exit code {result.returncode}")
    finally:
        # kaniko extracts the full base-image rootfs into the chroot — clean it up.
        shutil.rmtree(root, ignore_errors=True)
