"""Bias-corrected failure prevalence for a monitoring period."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np


def _validate_binary(values: Sequence[int], name: str) -> np.ndarray:
    items = list(values)
    if not items:
        raise ValueError(f"{name} must not be empty")
    for item in items:
        if isinstance(item, bool) or item not in (0, 1):
            raise ValueError(f"{name} must contain only 0 or 1, got {item!r}")
    return np.asarray(items, dtype=np.int8)


def corrected_mode_prevalence(
    sample_preds: Sequence[int],
    test_labels: Sequence[int],
    test_preds: Sequence[int],
    confidence: float = 0.95,
    bootstrap_iterations: int = 20000,
    seed: int | None = 7,
) -> dict[str, Any]:
    """Bias-corrected live prevalence for one mode from sampled verdicts.

    The contract, precisely:

      1. ``raw`` is the uncorrected flag rate: ``mean(sample_preds)``.
      2. Compute the frozen judge's failure sensitivity and pass specificity
         from ``test_labels`` and ``test_preds``. Both use the monitoring
         convention that 1 means a failure is present. Failure sensitivity is
         the flagged fraction of human-labeled failures. Pass specificity is
         the unflagged fraction of human-labeled passes.
      3. Compute the Rogan-Gladen point estimate, then resample the held-out
         records and sampled predictions to obtain a percentile-bootstrap
         interval. Use a seeded NumPy generator so the committed result is
         reproducible.
      4. Resample the monitoring predictions and the paired held-out records
         independently with replacement. Keep their original sample sizes.
         Discard a draw if the correction cannot be computed. Clamp each
         retained estimate to [0, 1], then take the percentile interval.
         Raise ``ValueError`` if no replicate is valid.

    Args:
        sample_preds: the judge's 0/1 verdicts over the UNIFORM BASE sample
            only (never the risk strata; they are biased toward failure by
            design).
        test_labels: human labels for the frozen Homework 5 judge's test
            split.
        test_preds: the frozen judge's predictions on that test split.
        confidence: interval confidence level.
        bootstrap_iterations: number of percentile-bootstrap replicates.
        seed: numpy seed for a reproducible interval; None leaves the RNG
            untouched.

    Returns:
        {"raw", "corrected", "ci_low", "ci_high", "confidence",
         "failure_sensitivity", "pass_specificity", "n_sample"}
        with "corrected" clamped to [0, 1] and rates rounded to 4 places.

    Raises:
        ValueError: if an input is empty, the held-out inputs have different
            lengths, a value is not 0 or 1, a class is absent, the judge is
            missing a usable correction, or no bootstrap replicate is valid.
    """
    sample_arr = _validate_binary(sample_preds, "sample_preds")
    label_arr = _validate_binary(test_labels, "test_labels")
    pred_arr = _validate_binary(test_preds, "test_preds")

    if len(label_arr) != len(pred_arr):
        raise ValueError("test_labels and test_preds must have the same length")
    if isinstance(confidence, bool) or not (0.0 < float(confidence) < 1.0):
        raise ValueError(f"confidence must be in (0, 1), got {confidence!r}")
    if isinstance(bootstrap_iterations, bool) or bootstrap_iterations < 1:
        raise ValueError("bootstrap_iterations must be at least 1")

    pos_mask = label_arr == 1
    neg_mask = label_arr == 0
    if not pos_mask.any() or not neg_mask.any():
        raise ValueError("test_labels must contain both failure (1) and pass (0) classes")

    raw = float(sample_arr.mean())
    failure_sensitivity = float(pred_arr[pos_mask].mean())
    pass_specificity = float((1 - pred_arr[neg_mask]).mean())
    denom = failure_sensitivity + pass_specificity - 1.0
    if denom <= 0.0:
        raise ValueError(
            "judge has no usable correction (failure_sensitivity + pass_specificity <= 1)"
        )
    corrected = float(min(max((raw + pass_specificity - 1.0) / denom, 0.0), 1.0))

    n_sample = len(sample_arr)
    n_test = len(label_arr)
    rng = np.random.default_rng(seed)

    retained_chunks: list[np.ndarray] = []
    chunk_size = 5000
    remaining = int(bootstrap_iterations)
    while remaining > 0:
        batch = min(chunk_size, remaining)
        remaining -= batch
        sample_idx = rng.integers(0, n_sample, size=(batch, n_sample))
        test_idx = rng.integers(0, n_test, size=(batch, n_test))

        boot_raw = sample_arr[sample_idx].mean(axis=1)
        boot_tl = label_arr[test_idx]
        boot_tp = pred_arr[test_idx]

        boot_pos = boot_tl == 1
        boot_neg = boot_tl == 0
        pos_cnt = boot_pos.sum(axis=1)
        neg_cnt = boot_neg.sum(axis=1)
        has_both = (pos_cnt > 0) & (neg_cnt > 0)
        if not has_both.any():
            continue

        boot_sens = (boot_pos[has_both] & (boot_tp[has_both] == 1)).sum(axis=1) / pos_cnt[has_both]
        boot_spec = (boot_neg[has_both] & (boot_tp[has_both] == 0)).sum(axis=1) / neg_cnt[has_both]
        boot_denom = boot_sens + boot_spec - 1.0
        usable = boot_denom > 0.0
        if not usable.any():
            continue

        boot_est = (boot_raw[has_both][usable] + boot_spec[usable] - 1.0) / boot_denom[usable]
        retained_chunks.append(np.clip(boot_est, 0.0, 1.0))

    if not retained_chunks:
        raise ValueError("no valid bootstrap replicates")

    retained = np.concatenate(retained_chunks)
    alpha = (1.0 - float(confidence)) / 2.0
    ci_low, ci_high = np.quantile(retained, [alpha, 1.0 - alpha])

    return {
        "raw": round(raw, 4),
        "corrected": round(corrected, 4),
        "ci_low": round(float(ci_low), 4),
        "ci_high": round(float(ci_high), 4),
        "confidence": float(confidence),
        "failure_sensitivity": round(failure_sensitivity, 4),
        "pass_specificity": round(pass_specificity, 4),
        "n_sample": n_sample,
    }
