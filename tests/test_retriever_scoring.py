"""
Regression tests for distance -> similarity conversion in rag/retriever.py.

Chroma defaults to hnsw:space="l2" when create_collection() is called without
an explicit space. The original build read those l2 distances as if they were
cosine distances (score = 1 - dist), which halved every score. With the 0.30
score floor that made retrieve() return [] for every query, so the agents never
received any documentation context.

These tests pin the conversion so that regression cannot return silently.
"""
import math

from rag.retriever import _SCORE_FLOOR, _similarity


def _l2_distance_for(cos_sim: float) -> float:
    """Squared L2 distance between L2-normalized vectors with this cosine sim."""
    return 2.0 - 2.0 * cos_sim


class TestSimilarityConversion:
    def test_l2_distance_recovers_true_cosine(self):
        # Embeddings are normalized, so squared L2 = 2 - 2*cos. Inverting it
        # must return the original cosine similarity.
        for cos in (0.0, 0.25, 0.5, 0.6327, 0.75, 1.0):
            dist = _l2_distance_for(cos)
            assert math.isclose(_similarity(dist, "l2"), cos, abs_tol=1e-9)

    def test_cosine_space_uses_one_minus_distance(self):
        # For space="cosine", Chroma returns distance = 1 - cos.
        for cos in (0.0, 0.3, 0.6327, 1.0):
            assert math.isclose(_similarity(1.0 - cos, "cosine"), cos, abs_tol=1e-9)

    def test_unknown_space_defaults_to_l2_not_cosine(self):
        # Chroma's default space is l2; an unrecognized value must not be
        # treated as cosine, which is what caused the original bug.
        dist = _l2_distance_for(0.6327)
        assert math.isclose(_similarity(dist, "something-else"), 0.6327, abs_tol=1e-9)

    def test_scores_are_clamped_to_unit_interval(self):
        assert _similarity(4.0, "l2") == 0.0      # cos = -1
        assert _similarity(0.0, "l2") == 1.0      # cos = 1
        assert _similarity(2.0, "cosine") == 0.0
        assert 0.0 <= _similarity(-0.2, "cosine") <= 1.0


class TestRegressionAgainstScoreFloor:
    def test_strong_l2_match_survives_the_score_floor(self):
        """
        The exact case from the bug report: 'Delta Lake time travel' matched
        delta-faq.mdx at Chroma distance 0.7347 (true cosine 0.6327). The old
        formula scored it 0.2653 and the 0.30 floor dropped it.
        """
        dist = 0.7347
        old_formula = 1.0 - dist
        assert old_formula < _SCORE_FLOOR, "precondition: old formula dropped this hit"

        score = _similarity(dist, "l2")
        assert math.isclose(score, 0.6327, abs_tol=1e-3)
        assert score >= _SCORE_FLOOR, "a strong match must survive the score floor"
