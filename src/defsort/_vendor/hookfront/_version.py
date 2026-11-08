"""Print installed distribution versions for tool CLIs."""

import importlib.metadata
import sys


def print_version(distribution: str) -> int:
    """Print the installed version, or explain missing metadata on stderr.

    Args:
        distribution: Installed distribution whose version is requested.

    Returns:
        Zero when metadata is available, otherwise two.

    Raises:
        OSError: If version output or the missing-metadata diagnostic cannot be
            written.
    """
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        print(
            f'error: installed distribution metadata for {distribution} is unavailable',
            file=sys.stderr,
        )
        return 2
    print(f'{distribution} {version}')
    return 0
