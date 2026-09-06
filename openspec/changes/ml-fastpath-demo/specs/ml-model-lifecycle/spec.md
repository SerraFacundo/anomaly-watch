## ADDED Requirements

### Requirement: Pinned model provenance
Every model used by the system SHALL be identified by a Hugging Face repository ID and an exact revision SHA recorded in configuration, never by a floating branch name.

#### Scenario: Model configured by repo and revision
- **WHEN** a model is configured for use by any service
- **THEN** the configuration SHALL specify both the repository ID and the exact revision SHA

#### Scenario: Deployed stack does not fetch at runtime
- **WHEN** the stack runs in a deployed environment
- **THEN** model weights SHALL be read from a pre-populated local cache directory with Hub access disabled, so that no model download occurs at runtime

#### Scenario: Permissive licensing enforced
- **WHEN** a model is proposed for use
- **THEN** it SHALL carry a permissive license compatible with self-hosted redistribution (apache-2.0 or MIT), and the license SHALL be recorded alongside the pin

### Requirement: Labeled evaluation dataset
The project SHALL maintain a reproducible dataset pipeline that produces a labeled train/validation/test split from public log corpora.

#### Scenario: Dataset built from LogHub
- **WHEN** the dataset preparation script is run
- **THEN** it SHALL produce labeled per-line examples derived from LogHub BGL and Thunderbird, where a `-` alert field denotes normal and any other value denotes an anomaly

#### Scenario: Chronological holdout
- **WHEN** the split is generated
- **THEN** the test split SHALL be held out chronologically rather than randomly, so that evaluation does not leak future log patterns into training

#### Scenario: Deterministic regeneration
- **WHEN** the preparation script is run twice with the same configuration
- **THEN** it SHALL produce byte-identical splits, and SHALL record corpus source, revision, row counts, and class balance in a manifest

### Requirement: Model evaluation harness
The project SHALL provide an evaluation harness that scores any candidate model against the fixed test split and reports a comparable set of metrics.

#### Scenario: Metrics reported per candidate
- **WHEN** the harness evaluates a candidate model
- **THEN** it SHALL report precision, recall, F1, PR-AUC, p95 single-record latency, p95 batch-of-32 latency, and on-disk model size

#### Scenario: Classical baseline included
- **WHEN** the harness is run
- **THEN** it SHALL include a non-transformer baseline over Drain3 template counts as a performance floor, so that transformer results are judged against a cheap alternative

#### Scenario: Results are persisted and comparable
- **WHEN** an evaluation run completes
- **THEN** results SHALL be written to a versioned results file recording model ID, revision, dataset manifest hash, and the metrics, so that runs are comparable across time

### Requirement: Fine-tuning procedure
The project SHALL provide a reproducible fine-tuning script that trains an open-weight encoder on the labeled split.

#### Scenario: Training runs on a single consumer GPU
- **WHEN** the fine-tuning script is run with default hyperparameters
- **THEN** it SHALL complete on a single consumer-grade GPU or a free-tier cloud GPU session, and SHALL not require multi-GPU or distributed training

#### Scenario: Training artifacts recorded
- **WHEN** a fine-tuning run completes
- **THEN** it SHALL emit a checkpoint together with the base model ID and revision, hyperparameters, dataset manifest hash, and training/validation curves

#### Scenario: CPU-only inference preserved
- **WHEN** a fine-tuned checkpoint is produced
- **THEN** it SHALL be servable on CPU without GPU hardware at the deployment site

### Requirement: Model promotion gate
A model SHALL become the configured default only after clearing an explicit quality and latency gate against the incumbent.

#### Scenario: Candidate outperforms incumbent
- **WHEN** a candidate model is proposed as the new default
- **THEN** it SHALL demonstrate a higher F1 than the incumbent on the fixed test split without exceeding the configured p95 latency budget

#### Scenario: Promotion is a configuration change
- **WHEN** a model clears the gate
- **THEN** promoting it SHALL require only updating the model ID and revision in configuration and restarting the service, with no code change

#### Scenario: Rollback to previous pin
- **WHEN** a promoted model behaves unacceptably
- **THEN** reverting the configuration to the previous pinned revision SHALL restore prior behavior with no data migration
