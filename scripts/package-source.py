"""Package current Git-listed source, including uncommitted release additions."""

import argparse
from pathlib import Path
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root.parent / "Smart-OA-Agent-source.zip")
    args = parser.parse_args()
    output = args.output.resolve()
    # Keeping the archive outside the repository prevents it entering later archives.
    if output.is_relative_to(root):
        parser.error("Output must be outside the repository directory")
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, check=True, capture_output=True,
    )
    paths = sorted(set(result.stdout.decode("utf-8").strip("\0").split("\0")))
    files = []
    for relative in paths:
        source = root / relative
        if not source.exists():  # A tracked local deletion should not be packaged.
            continue
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError(f"Unsafe source path: {relative}")
        name = source.name.lower()
        if (
            (name == ".env" or name.startswith(".env.")) and name != ".env.example"
            or source.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".dump", ".bak", ".log", ".zip"}
            or any(part in {".git", "node_modules", ".venv", "dist", "backups", ".idea"} for part in source.relative_to(root).parts)
        ):
            raise ValueError(f"Refusing to package sensitive or generated file: {relative}")
        files.append((relative, source))
    if not files:
        raise ValueError("No source files found")
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for relative, source in files:
            archive.write(source, f"Smart-OA-Agent/{relative}")
    with ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("Archive integrity check failed")
    print(f"Created {output} ({len(files)} files)")


if __name__ == "__main__":
    main()
