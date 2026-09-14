"""Setup script for the YmerFlow process/runner SDK."""

from setuptools import setup, find_packages

setup(
    name="ymerflow-process-sdk",
    version="0.1.0",
    description="YmerFlow runner harness (ENTRYPOINT + schema bake), the shared "
                "params_to_dockerfile function, and the create_environment process type.",
    packages=find_packages(include=["ymerflow_runner", "ymerflow_runner.*"]),
    python_requires=">=3.9",
    install_requires=[
        "fsspec",
        "s3fs",
        "gcsfs",
        "requests",
    ],
    entry_points={
        "ymerflow.process_types": [
            "create_environment=ymerflow_runner.create_environment:create_environment",
        ],
    },
)
