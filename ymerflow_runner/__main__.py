"""``python -m ymerflow_runner`` — the runner ENTRYPOINT baked into every environment image."""

from ymerflow_runner.runner import main

if __name__ == "__main__":
    main()
