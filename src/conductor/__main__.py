"""Entry point for running conductor as a module.

Usage:
    python -m conductor
"""

from conductor.cli.app import app


def main() -> None:
    """Main entry point for the conductor CLI."""
    app()


if __name__ == "__main__":
    main()
