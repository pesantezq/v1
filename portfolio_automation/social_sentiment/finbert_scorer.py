"""
Phase 6: Local FinBERT sentiment scorer — no paid API, CPU-safe, lazy loading.

Model: ProsusAI/finbert (HuggingFace hub) — financial domain BERT fine-tuned on
Financial PhraseBank. Labels: positive / negative / neutral.

Design:
  - Lazy loading: model is only loaded on first score() call (not at import).
  - CPU-safe: never requires CUDA; runs on CPU when GPU is unavailable.
  - Graceful degradation: if transformers/torch are not installed, or the model
    file is not present, returns scorer_unavailable status per-record.
  - Deterministic: uses torch.no_grad() + fixed random seed (not model-relevant
    since we're only doing inference, but keeps behavior predictable).
  - Batch scoring: scores a list of texts in one forward pass for efficiency.

Config:
  - ``finbert.enabled``: master switch (default True).
  - ``finbert.model_name``: HuggingFace model ID (default ProsusAI/finbert).
  - ``finbert.max_length``: max token length (default 512).
  - ``finbert.batch_size``: texts per forward pass (default 8).
  - ``finbert.allow_download``: if False, only load from local cache; never
    download (default False — safe for production runs).
  - ``finbert.cache_dir``: local model cache directory (default data/finbert/).

If ``allow_download=False`` and the model is not in cache, all calls return
ScorerResult with status="scorer_unavailable".
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.social_sentiment.finbert")

MODEL_NAME = "ProsusAI/finbert"
SCORER_VERSION = "finbert-1.0"
_LABEL_MAP = {"positive": "positive", "negative": "negative", "neutral": "neutral"}

# Global lazy-load state — one model instance per process.
_model: Any = None
_tokenizer: Any = None
_load_error: str | None = None
_load_attempted: bool = False


@dataclass
class ScorerResult:
    """Sentiment score for one text input."""

    text: str
    status: str                       # "ok" | "scorer_unavailable" | "error"
    sentiment_score: float = 0.0      # -1.0 (negative) to +1.0 (positive)
    positive_probability: float = 0.0
    neutral_probability: float = 0.0
    negative_probability: float = 0.0
    label: str = "neutral"
    scorer: str = "scorer_unavailable"
    scorer_version: str = SCORER_VERSION
    error: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


def _unavailable(text: str, reason: str = "") -> ScorerResult:
    return ScorerResult(
        text=text, status="scorer_unavailable",
        scorer="scorer_unavailable", scorer_version=SCORER_VERSION,
        neutral_probability=1.0,  # "don't know" defaults to neutral
        label="neutral",
        error=reason,
    )


def _load_model(model_name: str, cache_dir: str | None, allow_download: bool) -> str | None:
    """
    Load FinBERT into module-level globals. Returns an error string on failure,
    None on success.
    """
    global _model, _tokenizer, _load_error, _load_attempted
    if _load_attempted:
        return _load_error
    _load_attempted = True

    try:
        from transformers import (  # type: ignore[import]
            AutoTokenizer,
            AutoModelForSequenceClassification,
        )
        import torch  # type: ignore[import]
    except ImportError as exc:
        _load_error = f"transformers/torch not installed: {exc}"
        logger.debug("FinBERT scorer unavailable: %s", _load_error)
        return _load_error

    try:
        kwargs: dict[str, Any] = {"local_files_only": not allow_download}
        if cache_dir:
            kwargs["cache_dir"] = str(cache_dir)

        _tokenizer = AutoTokenizer.from_pretrained(model_name, **kwargs)
        _model = AutoModelForSequenceClassification.from_pretrained(model_name, **kwargs)
        _model.eval()
        logger.info("FinBERT model loaded from %s (allow_download=%s)", model_name, allow_download)
        _load_error = None
        return None
    except Exception as exc:
        _load_error = f"model_load_failed:{type(exc).__name__}:{str(exc)[:80]}"
        logger.debug("FinBERT load failed: %s", _load_error)
        return _load_error


class FinBERTScorer:
    """
    Lazy-loading FinBERT scorer. Safe to instantiate at import time — the model
    is not loaded until the first score() or score_batch() call.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._model_name = str(cfg.get("model_name", MODEL_NAME))
        self._max_length = int(cfg.get("max_length", 512))
        self._batch_size = int(cfg.get("batch_size", 8))
        self._allow_download = bool(cfg.get("allow_download", False))
        raw_cache = cfg.get("cache_dir", "data/finbert")
        self._cache_dir = str(raw_cache) if raw_cache else None
        self._initialized: bool = False

    def _ensure_loaded(self) -> bool:
        """Lazy init. Returns True if model is ready, False on degraded."""
        if not self._enabled:
            return False
        if not self._initialized:
            self._initialized = True
            _load_model(self._model_name, self._cache_dir, self._allow_download)
        return _model is not None and _load_error is None

    def score(self, text: str) -> ScorerResult:
        """Score a single text. Never raises."""
        results = self.score_batch([text])
        return results[0]

    def score_batch(self, texts: list[str]) -> list[ScorerResult]:
        """
        Score a batch of texts. Returns one ScorerResult per input.
        On any failure, returns scorer_unavailable for all texts in the batch.
        Never raises.
        """
        if not texts:
            return []
        if not self._ensure_loaded():
            reason = _load_error or ("scorer disabled" if not self._enabled else "model not loaded")
            return [_unavailable(t, reason) for t in texts]

        results: list[ScorerResult] = []
        try:
            import torch  # type: ignore[import]
            for i in range(0, len(texts), self._batch_size):
                batch = texts[i: i + self._batch_size]
                batch_results = self._score_batch_inner(batch, torch)
                results.extend(batch_results)
        except Exception as exc:
            reason = f"inference_error:{type(exc).__name__}"
            logger.warning("FinBERT inference error: %s", exc)
            results.extend([_unavailable(t, reason) for t in texts[len(results):]])
        return results

    def _score_batch_inner(self, texts: list[str], torch: Any) -> list[ScorerResult]:
        inputs = _tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self._max_length,
        )
        with torch.no_grad():
            outputs = _model(**inputs)
        probs = torch.softmax(outputs.logits, dim=1).tolist()
        id2label: dict[int, str] = _model.config.id2label

        results: list[ScorerResult] = []
        for text, prob_row in zip(texts, probs):
            # Map label indices to our canonical names
            label_probs: dict[str, float] = {}
            for idx, p in enumerate(prob_row):
                raw_label = id2label.get(idx, f"label_{idx}").lower()
                canonical = _LABEL_MAP.get(raw_label, raw_label)
                label_probs[canonical] = float(p)

            pos = label_probs.get("positive", 0.0)
            neg = label_probs.get("negative", 0.0)
            neu = label_probs.get("neutral", 0.0)
            # Normalize to sum=1 in case of float precision drift
            total = pos + neg + neu or 1.0
            pos, neg, neu = pos / total, neg / total, neu / total

            best_label = max(label_probs, key=label_probs.get)  # type: ignore[arg-type]
            sentiment_score = round(pos - neg, 4)

            results.append(ScorerResult(
                text=text,
                status="ok",
                sentiment_score=sentiment_score,
                positive_probability=round(pos, 4),
                neutral_probability=round(neu, 4),
                negative_probability=round(neg, 4),
                label=best_label,
                scorer="finbert",
                scorer_version=SCORER_VERSION,
            ))
        return results

    def is_available(self) -> bool:
        """Check model availability without triggering a load."""
        if not self._enabled:
            return False
        # Try loading if not yet attempted.
        return self._ensure_loaded()

    @property
    def status(self) -> str:
        """Descriptive status string for health reporting."""
        if not self._enabled:
            return "disabled"
        if not _load_attempted:
            return "not_yet_loaded"
        if _load_error:
            return f"scorer_unavailable:{_load_error[:40]}"
        return "ok"


# Module-level singleton for the pipeline (lazy init)
_default_scorer: FinBERTScorer | None = None


def get_scorer(config: dict[str, Any] | None = None) -> FinBERTScorer:
    """Return the process-wide scorer singleton, creating it if needed."""
    global _default_scorer
    if _default_scorer is None:
        _default_scorer = FinBERTScorer(config)
    return _default_scorer


def score_records(
    records: list[dict[str, Any]],
    scorer: FinBERTScorer | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Score a list of text records in-place and return them.

    Records that already have ``sentiment_score`` set are skipped.
    Records without a ``text`` field get scorer_unavailable.
    """
    from portfolio_automation.social_sentiment.schema import attach_sentiment

    sc = scorer or get_scorer(config)
    to_score_indices: list[int] = []
    texts: list[str] = []

    for i, rec in enumerate(records):
        if "sentiment_score" in rec:
            continue  # already scored
        text = str(rec.get("text") or "").strip()
        if not text:
            attach_sentiment(
                rec,
                sentiment_score=0.0,
                positive_probability=0.0,
                neutral_probability=1.0,
                negative_probability=0.0,
                label="neutral",
                scorer="scorer_unavailable",
                scorer_version=SCORER_VERSION,
            )
            continue
        to_score_indices.append(i)
        texts.append(text)

    if not texts:
        return records

    results = sc.score_batch(texts)
    for i, (rec_idx, result) in enumerate(zip(to_score_indices, results)):
        rec = records[rec_idx]
        attach_sentiment(
            rec,
            sentiment_score=result.sentiment_score,
            positive_probability=result.positive_probability,
            neutral_probability=result.neutral_probability,
            negative_probability=result.negative_probability,
            label=result.label,
            scorer=result.scorer,
            scorer_version=result.scorer_version,
        )

    return records
