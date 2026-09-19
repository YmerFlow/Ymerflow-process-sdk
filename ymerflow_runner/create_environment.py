"""``create_environment`` process type.

Builds a custom Docker environment (base image + extra pip packages + extra Dockerfile
instructions) with in-pod kaniko, extracts its ``process_schemas.json`` with crane, and writes an
``environment.json`` to storage that the backend picks up (in ``_create_outputs``) to register a
reusable Environment other processes can run in.

The Dockerfile is synthesized by the shared :func:`ymerflow_runner.params_to_dockerfile` — the same
function ``docker/build.sh`` uses for the default base runner — so custom environments and the base
runner never drift. The form's field defaults are seeded from the ``BASE_RUNNER_PARAMS_JSON`` env
var baked into the runner image, so it opens pre-populated with the deployment's actual base-runner
params.
"""
import json
import os
import re
import subprocess
import tarfile
import tempfile

import fsspec

from ymerflow_runner.params_to_dockerfile import params_to_dockerfile
from ymerflow_runner.kaniko_build import kaniko_build


def _default_params():
    """The base-runner param set baked into the image as ``BASE_RUNNER_PARAMS_JSON`` (by
    ``docker/build.sh``). Empty when unset, so the form still renders with blank defaults."""
    return json.loads(os.environ.get("BASE_RUNNER_PARAMS_JSON") or "{}")


class create_environment:
    """Create environment process type."""

    @classmethod
    def schema(cls):
        """Return JSON Schema for create_environment parameters.

        Field defaults come from ``BASE_RUNNER_PARAMS_JSON`` (the params this deployment's base
        runner was built with) so the form opens ready to reproduce or extend the base runner.
        """
        params = _default_params()
        return {
            "type": "object",
            "title": "Create Environment",
            "description": "Build a custom Docker environment from a base image plus extra Python "
                           "packages and Dockerfile instructions. Produces a reusable environment "
                           "that other processes can run in.",
            "properties": {
                "environment_name": {
                    "type": "string",
                    "title": "Environment Name",
                    "description": "Name for the environment (will be slugified for Docker image)"
                },
                "base_image": {
                    "type": "string",
                    "title": "Base Docker Image",
                    "default": params.get("base_image", "python:3.11-slim"),
                    "description": "Base Docker image to build from"
                },
                "python_packages": {
                    "type": "string",
                    "title": "Python Packages",
                    "default": params.get("python_packages", ""),
                    "description": "Python packages to install, one per line (requirements.txt "
                                   "format). Blank lines and #comments are ignored; git URLs are "
                                   "allowed, e.g. `git+https://github.com/org/repo.git` or "
                                   "`pkg[extra] @ git+https://github.com/org/repo.git`.",
                    "x-format": "textarea"
                },
                "dockerfile_instructions": {
                    "type": "string",
                    "title": "Additional Dockerfile Instructions",
                    "default": params.get("dockerfile_instructions", ""),
                    "description": "Extra Dockerfile lines run BEFORE the pip install — put "
                                   "system-level setup here (e.g. apt build tools a package "
                                   "compiles against).",
                    "x-format": "textarea"
                },
                "install_kaniko": {
                    "type": "boolean",
                    "title": "Install Kaniko",
                    "default": params.get("install_kaniko", False),
                    "description": "Include the kaniko executor so this environment can itself "
                                   "build images (e.g. to run create_environment)."
                }
            },
            "required": ["environment_name", "base_image"]
        }

    @classmethod
    def run(cls, storage_context=None, **kwargs):
        """Create environment by building and pushing Docker image."""
        print("Creating environment...")
        print(f"Parameters: {kwargs}")

        # Get parameters
        environment_name = kwargs.get('environment_name', 'unnamed-env')
        base_image = kwargs.get('base_image', 'python:3.11-slim')
        python_packages = kwargs.get('python_packages', '').strip()
        dockerfile_instructions = kwargs.get('dockerfile_instructions', '').strip()
        install_kaniko = bool(kwargs.get('install_kaniko', False))

        # Get context
        process_id = storage_context['process_id']
        project_id = storage_context['project_id']
        version = os.environ.get('VERSION', '1')
        registry_url = os.environ.get('REGISTRY_URL', '')
        registry_auth = os.environ.get('REGISTRY_AUTH', '')

        if not registry_url:
            raise ValueError("REGISTRY_URL environment variable not set")

        # ─── kaniko / go-containerregistry is broken by design; work around its idiocy ────────────
        # REGISTRY_URL carries the explicit HTTPS port, e.g. "registry.ymerflow.earth:443". kaniko
        # opens the push against that host:port, but our registry (behind nginx) hands back the
        # blob-upload Location on the BARE host "registry.ymerflow.earth" — 443 is the scheme default
        # so it's omitted, as every sane HTTP tool does. Go's stdlib http client — and therefore
        # go-containerregistry, which kaniko pushes with — decides that "registry.ymerflow.earth:443"
        # and "registry.ymerflow.earth" are DIFFERENT HOSTS, and helpfully strips the Authorization
        # header on the "cross-host" redirect. So the blob-upload PATCH goes out ANONYMOUS and the
        # registry rightly answers 401 UNAUTHORIZED — with credentials that are completely valid.
        # crane, containerd, and a plain `docker push` all follow the very same redirect without
        # losing auth; only kaniko/go manages to shoot itself in the foot here. Verified against the
        # real prod registry: identical creds, :443 host → 401 on the PATCH; bare host end-to-end →
        # clean push. The cure for a self-inflicted "the port changed so it's a new host" wound is to
        # never let the port change: address the registry by the bare host (drop a redundant,
        # scheme-default :443) for everything kaniko and crane touch, so the flow never mutates
        # "host" mid-upload. The stored ref below keeps the :443 form on purpose — pods pull it via
        # containerd (which is not broken and tolerates the redirect) and the imagePullSecret is
        # keyed to that host:port. Same registry, same repo/tag, just addressed without the dead port.
        push_registry_url = registry_url[:-len(':443')] if registry_url.endswith(':443') else registry_url

        # Slugify environment name
        env_slug = re.sub(r'[^a-z0-9-]', '-', environment_name.lower()).strip('-')
        if not env_slug:
            env_slug = 'unnamed-env'

        # Build image tag: {registry_url}/proj-{project_id}/env-{slug}:{process_id}-{version}
        image_repository = f"{registry_url}/proj-{project_id}/env-{env_slug}"
        image_tag = f"{process_id}-{version}"
        full_image_name = f"{image_repository}:{image_tag}"  # stored/returned ref — pods pull this
        # What kaniko pushes to and crane pulls from: bare host, per the broken-by-design note above.
        push_image_name = f"{push_registry_url}/proj-{project_id}/env-{env_slug}:{image_tag}"

        print(f"Building image: {full_image_name}")

        # If base_image doesn't have a registry prefix, add the local registry for ymerflow-* images
        if base_image and not base_image.startswith(('registry:', 'localhost:', 'gcr.io/', 'docker.io/')):
            if base_image.startswith('ymerflow-'):
                base_image_with_registry = f"{registry_url}/{base_image}"
            else:
                # External image from Docker Hub, use as-is
                base_image_with_registry = base_image
        else:
            base_image_with_registry = base_image

        dockerfile_content = params_to_dockerfile(
            base_image_with_registry,
            python_packages=python_packages,
            dockerfile_instructions=dockerfile_instructions,
            install_kaniko=install_kaniko,
        )

        print("Dockerfile content:")
        print("---")
        print(dockerfile_content)
        print("---")

        # Build and push with kaniko, isolated in a chroot so it can't corrupt this runner pod's own
        # filesystem/site-packages (ymerflow_runner.kaniko_build; see
        # docs/plans/fix-kaniko-clobbers-runner-filesystem.md). Kaniko builds by unpacking the target
        # image's rootfs onto its own container's / — the chroot contains that wipe entirely, so this
        # process's crane binary and site-packages stay pristine and need no --ignore-path survival
        # hacks. The /kaniko + KANIKO_DIR reservation dance lives inside the helper's chroot now.
        # Push to the BARE host (push_image_name) and key auth to it — see the broken-by-design :443
        # note above; the dev registry needs the insecure flags.
        print(f"Running Kaniko to build and push image...")
        kaniko_build(
            dockerfile_content,
            push_image_name,
            registry_url=push_registry_url,
            registry_auth=registry_auth or None,
            extra_args=[
                '--insecure',  # For dev registry without TLS
                '--skip-tls-verify',  # For dev registry
                '--insecure-pull',  # Allow pulling from insecure registries
                f'--skip-tls-verify-registry={push_registry_url}',  # Skip TLS for pulling from our registry
            ],
        )
        print(f"✓ Image built and pushed successfully: {full_image_name}")

        # Extract process_schemas.json from the built image using crane, in this (now pristine)
        # parent process. crane reads registry auth from DOCKER_CONFIG. Pull from the bare push host,
        # not full_image_name: crane isn't go/kaniko-broken, but the :443 form would drag it through
        # the same port-only "host change" on the blob GETs. No timeout: exporting a multi-GB image
        # can take minutes and the Job's activeDeadlineSeconds already bounds it.
        print(f"Extracting process schemas from image...")
        with tempfile.TemporaryDirectory() as tmpdir:
            if registry_auth:
                docker_config_dir = os.path.join(tmpdir, '.docker')
                os.makedirs(docker_config_dir, exist_ok=True)
                config_content = {"auths": {push_registry_url: {"auth": registry_auth}}}
                with open(os.path.join(docker_config_dir, 'config.json'), 'w') as f:
                    json.dump(config_content, f)
                os.environ['DOCKER_CONFIG'] = docker_config_dir

            tar_path = os.path.join(tmpdir, 'image.tar')
            crane_result = subprocess.run(
                ['crane', 'export', '--insecure', push_image_name, tar_path],
                capture_output=True,
                text=True,
            )

            if crane_result.returncode != 0:
                raise RuntimeError(
                    f"crane export failed for {full_image_name} "
                    f"(exit {crane_result.returncode}): {crane_result.stderr.strip()}"
                )

            with tarfile.open(tar_path) as tar:
                try:
                    member = tar.getmember('app/process_schemas.json')
                except KeyError:
                    raise RuntimeError(
                        f"app/process_schemas.json not found in image {full_image_name}"
                    )
                with tar.extractfile(member) as f:
                    process_schemas = json.loads(f.read().decode('utf-8'))

            print(f"✓ Extracted process schemas: {list(process_schemas.keys())}")

        # Write environment info to storage (will be picked up by _create_outputs)
        print(f"Writing environment info to storage...")

        environment_info = {
            "name": environment_name,
            "docker_image": full_image_name,
            "process_id": process_id,
            "process_types": process_schemas
        }

        storage_base = storage_context['storage_base']
        storage_kwargs = storage_context['storage_kwargs']
        env_info_url = f"{storage_base}/processes/{process_id}/environment.json"

        print(f"Writing to: {env_info_url}")

        with fsspec.open(env_info_url, 'w', **storage_kwargs) as f:
            json.dump(environment_info, f, indent=2)

        print(f"✓ Environment info written to storage")

        print("Environment creation complete")
        return {
            "status": "success",
            "image": full_image_name,
            "environment_name": environment_name,
            "process_types": list(process_schemas.keys())
        }
