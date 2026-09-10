"""Model loading and scoring, shared by the service, the smoke test and the eval harness.

One implementation so that the number the harness reports is produced by exactly
the same code path that serves traffic.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from registry import DEFAULT_CACHE_DIR, ModelPin, get_pin

# Off-the-shelf checkpoints disagree about which class index means "anomaly" and
# several ship no id2label at all. Guessing wrong silently inverts every score,
# so resolution is explicit and logged.
_ANOMALY_LABEL_PATTERN = re.compile(r"anomal|abnormal|alert|error|attack|malicious|positive", re.IGNORECASE)
_NORMAL_LABEL_PATTERN = re.compile(r"normal|benign|negative|ok\b", re.IGNORECASE)


@dataclass(frozen=True)
class ScoreResult:
    score: float
    is_anomaly: bool


class LabelResolutionError(RuntimeError):
    pass


def resolve_anomaly_index(id2label: dict[int, str] | None, override: int | None = None) -> tuple[int, str]:
    """Return (index, how_it_was_decided) for the anomaly class."""
    if override is not None:
        return override, f"explicit override index={override}"

    if id2label:
        named = {int(k): str(v) for k, v in id2label.items()}
        hits = [i for i, label in named.items() if _ANOMALY_LABEL_PATTERN.search(label)]
        if len(hits) == 1:
            return hits[0], f"matched label {named[hits[0]]!r}"
        # Some checkpoints only name the negative class meaningfully.
        normals = [i for i, label in named.items() if _NORMAL_LABEL_PATTERN.search(label)]
        if len(normals) == 1 and len(named) == 2:
            other = next(i for i in named if i != normals[0])
            return other, f"inferred as the complement of {named[normals[0]]!r}"

    # LABEL_0/LABEL_1 placeholders carry no meaning. Fall back to the binary
    # convention (1 == positive == anomaly) but say so loudly.
    return 1, "fallback: unlabeled binary head, assuming index 1 is the positive class"


class LogScorer:
    """Binary anomaly classifier over raw log messages.

    Scores the raw message, not the Drain3 template id — the pretrained
    checkpoints were trained on natural-language text, and feeding them opaque
    template ids throws away the pretraining that makes them worth using.
    """

    def __init__(
        self,
        pin: ModelPin,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        threshold: float = 0.5,
        max_length: int = 256,
        num_threads: int | None = None,
        anomaly_index_override: int | None = None,
        offline: bool = True,
        device: str | None = None,
    ) -> None:
        import torch
        from transformers import AutoConfig, AutoModelForSequenceClassification, AutoTokenizer

        self.pin = pin
        self.threshold = threshold
        self.max_length = max_length
        self._torch = torch

        if num_threads:
            # Bounded so the detector cannot starve co-located stack services on a
            # single PyME host.
            torch.set_num_threads(num_threads)

        revision = pin.require_revision()

        # A locally-trained checkpoint lives in the cache directory alongside the
        # Hub downloads, and is named by directory rather than by `org/name`.
        # Resolving it against `cache_dir` is what lets one registry entry work
        # both from a `uv run` at the repo root and inside the container, where
        # the same directory is mounted at a different path. Without this the
        # entry has to name a path that is correct in exactly one of the two.
        local_checkpoint = Path(cache_dir) / pin.repo_id
        source = str(local_checkpoint) if local_checkpoint.is_dir() else pin.repo_id

        load_kwargs = {
            "pretrained_model_name_or_path": source,
            "revision": revision,
            "cache_dir": str(cache_dir),
            "local_files_only": offline,
        }

        try:
            config = AutoConfig.from_pretrained(**load_kwargs)
            self.tokenizer = AutoTokenizer.from_pretrained(**load_kwargs)
            self.model = AutoModelForSequenceClassification.from_pretrained(**load_kwargs)
        except OSError as exc:
            raise FileNotFoundError(
                f"Model '{pin.repo_id}' at revision {revision} is not in the local cache "
                f"({cache_dir}) and Hub access is {'disabled' if offline else 'enabled'}. "
                f"Run: python ml/download_models.py --model {pin.key}"
            ) from exc

        self.model.eval()
        # Default "cpu", not auto-detected: detector-service must score on the
        # exact hardware it was measured on (a PyME host has no GPU), so a
        # caller that says nothing gets exactly today's behavior. Only the eval
        # harness passes an explicit device, to use a GPU when one is sitting
        # idle underneath it — running a comparison run's inference on CPU
        # while a GPU goes unused was the actual bottleneck on a Colab session.
        self.device = device or "cpu"
        self.model.to(self.device)
        # Precedence: caller override > registry pin > checkpoint id2label > fallback.
        self.anomaly_index, self.anomaly_index_reason = resolve_anomaly_index(
            getattr(config, "id2label", None),
            anomaly_index_override if anomaly_index_override is not None else pin.anomaly_index,
        )
        self.num_labels = int(getattr(config, "num_labels", 2))
        if self.anomaly_index >= self.num_labels:
            raise LabelResolutionError(
                f"Anomaly index {self.anomaly_index} is out of range for a "
                f"{self.num_labels}-class head on {pin.repo_id}."
            )

    def score(self, messages: Sequence[str]) -> list[ScoreResult]:
        if not messages:
            return []

        torch = self._torch
        batch = self.tokenizer(
            list(messages),
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        batch = {name: tensor.to(self.device) for name, tensor in batch.items()}
        with torch.inference_mode():
            logits = self.model(**batch).logits
            probabilities = torch.softmax(logits, dim=-1)[:, self.anomaly_index]

        return [
            ScoreResult(score=float(p), is_anomaly=bool(p >= self.threshold))
            for p in probabilities.tolist()
        ]

    def metadata(self) -> dict[str, object]:
        return {
            "key": self.pin.key,
            "repo_id": self.pin.repo_id,
            "revision": self.pin.revision,
            "license": self.pin.license,
            "role": self.pin.role,
            "threshold": self.threshold,
            "max_length": self.max_length,
            "anomaly_index": self.anomaly_index,
            "anomaly_index_reason": self.anomaly_index_reason,
        }


def load_default_scorer(**kwargs) -> LogScorer:
    key = os.environ.get("DETECTOR_MODEL_KEY") or None
    return LogScorer(get_pin(key), **kwargs)
