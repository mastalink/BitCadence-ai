"""Resolve static argv files and compare their contents with the owner-managed lock."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from .contract import Command, Manifest


MANIFEST_NAME = "bitcadence-plugin.toml"
LOCK_NAME = "plugins.lock.toml"
DIGEST = re.compile(r"[0-9a-f]{64}")
SCRIPT_SUFFIXES = {
    ".bat", ".cmd", ".dll", ".jar", ".js", ".mjs", ".pl", ".ps1", ".py", ".pyw",
    ".rb", ".sh", ".so", ".vbs", ".wasm",
}
SHELL_WRAPPERS = {
    "cmd", "command", "powershell", "pwsh", "wscript", "cscript", "env",
    "sh", "bash", "zsh", "dash", "ksh", "fish", "busybox",
}
INTERPRETERS = {"node", "ruby", "perl", "bun", "deno", "lua", "php"}


class TrustError(ValueError):
    """A candidate cannot be identified and must not run."""


def canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FilePin:
    path: str
    sha256: str


@dataclass(frozen=True)
class Candidate:
    manifest: Manifest
    source: Path
    folder: Path
    digest: str
    argv: dict[str, tuple[str, ...]]
    files: dict[str, FilePin]

    @property
    def fingerprint(self) -> str:
        files = {key: [pin.path, pin.sha256] for key, pin in self.files.items()}
        return hashlib.sha256(canonical([self.digest, files])).hexdigest()


def _regular_file(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise TrustError("command file must be a regular file")
    return resolved


def _resolve_command(command: Command, root: Path) -> tuple[tuple[str, ...], dict[int, Path]]:
    raw = list(command.argv)
    executable = Path(raw[0])
    local = root / executable
    if executable.is_absolute() or any(char in raw[0] for char in "/\\") or local.is_file():
        target = _regular_file(local)
    else:
        located = shutil.which(raw[0])
        if located is None:
            raise TrustError("executable could not be resolved")
        target = _regular_file(Path(located))
    if os.name == "nt" and target.suffix.lower() != ".exe":
        raise TrustError("Windows plugin commands require a native .exe executable")
    with target.open("rb") as stream:
        if stream.read(2) == b"#!":
            raise TrustError("declare the script interpreter explicitly so it can be pinned")
    name = target.stem.lower()
    if name in SHELL_WRAPPERS:
        raise TrustError("shell and environment wrappers are not plugin executables")
    raw[0] = str(target)
    files = {0: target}
    for index, arg in enumerate(command.argv[1:], 1):
        prefix, sep, value = arg.partition("=") if arg.startswith("-") else ("", "", arg)
        if not sep:
            value = arg
        path = root / value
        looks_like_file = Path(value).suffix.lower() in SCRIPT_SUFFIXES
        if index in command.script_args or looks_like_file or (value and path.is_file()):
            files[index] = _regular_file(path)
            raw[index] = prefix + sep + str(files[index])
        elif value and any(char in value for char in "/\\") and not path.is_dir():
            raise TrustError("path arguments must resolve to a file or directory")
    # Known interpreters use a pinned script as their entry point. In particular,
    # parameters cannot become flags to an interpreter or the script to execute.
    python = re.fullmatch(r"python(?:w|[0-9.]+)?", name)
    if python or name in INTERPRETERS:
        if 1 not in files or command.argv[1].startswith("-"):
            raise TrustError("an interpreter requires a pinned script immediately after argv[0]")
        if python:
            # Isolated mode ignores PYTHONPATH and implicit cwd/script imports.
            raw.insert(1, "-I")
    return tuple(raw), files


def inspect_plugin(path: Path, *, hashes: dict[Path, str] | None = None) -> Candidate:
    source = path.absolute()
    path = source.resolve(strict=True)
    raw = path.read_bytes()
    model = Manifest.model_validate(tomllib.loads(raw.decode("utf-8")))
    files: dict[str, FilePin] = {}
    commands: dict[str, tuple[str, ...]] = {}
    digests = {} if hashes is None else hashes
    for key, command in model.commands().items():
        commands[key], resolved = _resolve_command(command, path.parent)
        for index, file in resolved.items():
            if file not in digests:
                digests[file] = sha256_file(file)
            files[f"{key}/{index}"] = FilePin(str(file), digests[file])
    return Candidate(model, source, path.parent, hashlib.sha256(raw).hexdigest(), commands, files)


def discover(directory: Path) -> list[Path]:
    return sorted(directory.glob("*/" + MANIFEST_NAME))


def read_lock(path: Path) -> dict[str, dict]:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    if document.get("version") != 1 or not isinstance(document.get("plugins"), dict):
        raise TrustError("invalid plugin lock")
    for entry in document["plugins"].values():
        if not isinstance(entry, dict) or not isinstance(entry.get("files"), dict):
            raise TrustError("invalid plugin lock entry")
        if not DIGEST.fullmatch(str(entry.get("manifest_sha256", ""))):
            raise TrustError("invalid manifest pin")
        for pin in entry["files"].values():
            if (not isinstance(pin, dict) or not isinstance(pin.get("path"), str)
                    or not DIGEST.fullmatch(str(pin.get("sha256", "")))):
                raise TrustError("invalid executable pin")
    return document["plugins"]


def trust_changes(candidate: Candidate, entry: dict | None) -> dict[str, dict]:
    if entry is None:
        return {"approval": {"approved": None, "current": candidate.fingerprint}}
    changes = {}
    if entry["manifest_sha256"] != candidate.digest:
        changes["manifest"] = {"approved": entry["manifest_sha256"], "current": candidate.digest}
    old = entry["files"]
    current = {key: {"path": pin.path, "sha256": pin.sha256} for key, pin in candidate.files.items()}
    for key in sorted(old.keys() | current.keys()):
        if old.get(key) != current.get(key):
            pin = current.get(key) or old[key]
            label = f"{key}:{Path(pin['path']).name}"
            changes[label] = {"approved": old.get(key), "current": current.get(key)}
    return changes


def trust_text(candidate: Candidate) -> str:
    """Print an approval candidate. Callers must review and explicitly install it."""
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    key = quote(candidate.manifest.id)
    lines = ["version = 1", "", f"[plugins.{key}]",
             f"manifest_sha256 = {quote(candidate.digest)}", "", f"[plugins.{key}.files]"]
    for name, pin in sorted(candidate.files.items()):
        lines.append(f"{quote(name)} = {{ path = {quote(pin.path)}, sha256 = {quote(pin.sha256)} }}")
    return "\n".join(lines) + "\n"
