"""YmerFlow process/runner SDK.

The runner harness (``python -m ymerflow_runner`` ENTRYPOINT, ``ymerflow_runner.get_schema``
build-time schema bake) plus ``params_to_dockerfile`` — the single Dockerfile-synthesis function
shared by ``docker/build.sh`` (host build of the default base runner) and the ``create_environment``
process type (in-pod kaniko build of custom environments), so the two never drift.
"""

__version__ = "0.1.0"
