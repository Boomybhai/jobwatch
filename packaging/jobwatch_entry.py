"""Entry point PyInstaller turns into jobwatch.exe / jobwatch-background.exe."""
import sys

from jobwatch.cli import main

if __name__ == "__main__":
    sys.exit(main())
