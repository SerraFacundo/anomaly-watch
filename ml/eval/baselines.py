"""Non-transformer baselines — the floor any model has to clear to be worth its cost.

Two of them, and the second one matters more than it looks:

- `template-count`: Drain3 templates + IsolationForest. The classical approach.
  If a 110M-parameter transformer cannot beat counting templates, that is worth
  knowing before anyone books GPU time.

- `level-keyword`: reads the severity word and nothing else. BGL's Level field
  predicts the label almost perfectly, so this baseline usually posts a very high
  F1. It is included precisely because a high number here means the *task* is
  easy, not that a model is good. Any transformer result should be read against
  this line, not against the template-count line.
"""

from __future__ import annotations

import re
from typing import Sequence

# Severity words that appear inside log content across most systems.
_SEVERITY_PATTERN = re.compile(
    r"\b(FATAL|SEVERE|CRITICAL|CRIT|PANIC|EMERG|ALERT|ERROR|ERR|FAIL(?:ED|URE)?|EXCEPTION)\b",
    re.IGNORECASE,
)


def level_keyword_scores(messages: Sequence[str]) -> list[float]:
    """1.0 if the message contains a severity keyword, else 0.0."""
    return [1.0 if _SEVERITY_PATTERN.search(message) else 0.0 for message in messages]


def template_count_scores(
    train_messages: Sequence[str],
    eval_messages: Sequence[str],
    contamination: float = 0.1,
    random_state: int = 0,
) -> list[float]:
    """Drain3 template id + rarity features -> IsolationForest anomaly score.

    Drain3 is fitted on the training messages only, so templates first seen at
    evaluation time are genuinely unseen — which is itself the strongest signal
    this baseline has.
    """
    import numpy as np
    from drain3 import TemplateMiner
    from drain3.template_miner_config import TemplateMinerConfig
    from sklearn.ensemble import IsolationForest

    config = TemplateMinerConfig()
    config.drain_sim_th = 0.4
    miner = TemplateMiner(config=config)

    train_template_counts: dict[str, int] = {}
    for message in train_messages:
        result = miner.add_log_message(message)
        cluster_id = str(result.get("cluster_id"))
        train_template_counts[cluster_id] = train_template_counts.get(cluster_id, 0) + 1

    total = sum(train_template_counts.values()) or 1

    def featurize(message: str, learn: bool) -> list[float]:
        if learn:
            result = miner.add_log_message(message)
            cluster_id = str(result.get("cluster_id"))
        else:
            match = miner.match(message)
            cluster_id = str(match.cluster_id) if match else "-1"
        frequency = train_template_counts.get(cluster_id, 0) / total
        tokens = message.split()
        return [
            frequency,
            1.0 if cluster_id == "-1" else 0.0,  # unseen template
            float(len(tokens)),
            float(len(message)),
            float(sum(token.isdigit() for token in tokens)),
        ]

    train_features = np.array([featurize(message, learn=False) for message in train_messages])
    eval_features = np.array([featurize(message, learn=False) for message in eval_messages])

    forest = IsolationForest(contamination=contamination, random_state=random_state, n_estimators=100)
    forest.fit(train_features)

    # decision_function: higher = more normal. Flip and min-max to [0, 1] so the
    # score means the same thing as a classifier probability.
    raw = -forest.decision_function(eval_features)
    low, high = float(raw.min()), float(raw.max())
    if high - low < 1e-12:
        return [0.0] * len(eval_messages)
    return ((raw - low) / (high - low)).tolist()
