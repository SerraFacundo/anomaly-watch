"""Read/write access to ml/models.yaml.

Kept separate from the downloader so the detector service can resolve a pin
without importing anything that talks to the Hub.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REGISTRY_PATH = Path(__file__).resolve().parent / "models.yaml"

# Where downloaded weights live. Mounted as a Docker volume in the stack, so the
# image stays small and models can be swapped without a rebuild.
DEFAULT_CACHE_DIR = Path(
    os.environ.get("ANOMALY_WATCH_MODEL_CACHE", Path(__file__).resolve().parents[1] / "models" / "cache")
)


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelPin:
    key: str
    repo_id: str
    revision: str | None
    license: str
    role: str
    task: str | None = None
    dimensions: int | None = None
    notes: str | None = None
    # Which output index means "anomaly". Set explicitly when a checkpoint ships
    # no id2label, because guessing silently inverts every score it produces.
    anomaly_index: int | None = None

    def require_revision(self) -> str:
        if not self.revision:
            raise RegistryError(
                f"Model '{self.key}' ({self.repo_id}) has no pinned revision. "
                f"Run: python ml/download_models.py --model {self.key}"
            )
        return self.revision


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def get_pin(key: str | None = None, path: Path = REGISTRY_PATH) -> ModelPin:
    data = load_registry(path)
    key = key or data.get("default")
    models = data.get("models", {})
    if key not in models:
        raise RegistryError(f"Unknown model key '{key}'. Known keys: {sorted(models)}")
    entry = models[key]
    return ModelPin(
        key=key,
        repo_id=entry["repo_id"],
        revision=entry.get("revision"),
        license=entry.get("license", "unknown"),
        role=entry.get("role", "unknown"),
        task=entry.get("task"),
        dimensions=entry.get("dimensions"),
        notes=entry.get("notes"),
        anomaly_index=entry.get("anomaly_index"),
    )


def iter_pins(roles: set[str] | None = None, path: Path = REGISTRY_PATH) -> list[ModelPin]:
    data = load_registry(path)
    pins = [get_pin(key, path) for key in data.get("models", {})]
    if roles is not None:
        pins = [pin for pin in pins if pin.role in roles]
    return pins


def set_revision(key: str, revision: str, path: Path = REGISTRY_PATH) -> None:
    """Write a resolved SHA back into the registry, preserving comments.

    yaml.safe_dump would strip every comment in the file, and those comments are
    the reasoning behind the pins. So patch the one line instead.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_target = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            in_target = True
            continue
        if in_target:
            if stripped.startswith("revision:"):
                indent = len(line) - len(line.lstrip())
                lines[index] = f"{' ' * indent}revision: {revision}\n"
                path.write_text("".join(lines), encoding="utf-8")
                return
            # Left the block without finding the key.
            if stripped and not line[0].isspace():
                break
    raise RegistryError(f"No 'revision:' field found for model '{key}' in {path}")
