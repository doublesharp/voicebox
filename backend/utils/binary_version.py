"""Validate output from a Voicebox server binary version probe."""


def parse_server_version(returncode: int, stdout: str) -> str | None:
    """Return the single version emitted by a successfully exited server."""
    if returncode != 0:
        return None
    versions = []
    for line in stdout.splitlines():
        words = line.split()
        if len(words) == 2 and words[0] == 'voicebox-server':
            versions.append(words[1])
    return versions[0] if len(versions) == 1 else None
