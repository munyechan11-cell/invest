"""Portfolio optimizers.

Pure NumPy — no cvxpy/scipy.optimize dependency, because the problems here are
small and closed-form or trivially projected-gradient. Every routine returns
long-only weights summing to 1; the caller re-applies the insight's sign.

Covariance is always Ledoit-Wolf-style shrunk toward a scaled identity. Sample
covariance on 60 daily observations of 20 assets is near-singular, and an
unshrunk inverse produces the famous "optimizer output that is 400% long one
name and 380% short another".
"""
from __future__ import annotations

import numpy as np


def _cov(returns: list[list[float]], shrinkage: float = 0.2) -> np.ndarray:
    arr = np.asarray(returns, dtype=float)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    sample = np.cov(arr, ddof=1)
    if sample.ndim == 0:
        sample = sample.reshape(1, 1)
    n = sample.shape[0]
    target = np.eye(n) * np.trace(sample) / n
    shrunk = (1.0 - shrinkage) * sample + shrinkage * target
    # tiny ridge so the matrix is invertible even in pathological cases
    return shrunk + np.eye(n) * 1e-10


def _normalize(w: np.ndarray) -> list[float]:
    w = np.clip(np.nan_to_num(w, nan=0.0), 0.0, None)
    total = w.sum()
    if total <= 0:
        return [1.0 / len(w)] * len(w)
    return (w / total).tolist()


def inverse_variance_weights(returns: list[list[float]]) -> list[float]:
    """Naive risk parity: ignores correlation, and is remarkably hard to beat."""
    var = np.var(np.asarray(returns, dtype=float), axis=1, ddof=1)
    var = np.where(var <= 0, np.nanmean(var[var > 0]) if np.any(var > 0) else 1.0, var)
    return _normalize(1.0 / var)


def min_variance_weights(returns: list[list[float]], shrinkage: float = 0.2) -> list[float]:
    cov = _cov(returns, shrinkage)
    ones = np.ones(cov.shape[0])
    try:
        raw = np.linalg.solve(cov, ones)
    except np.linalg.LinAlgError:
        return inverse_variance_weights(returns)
    return _normalize(raw)


def max_sharpe_weights(
    returns: list[list[float]],
    expected_returns: list[float],
    risk_free_rate: float = 0.0,
    shrinkage: float = 0.2,
) -> list[float]:
    """Tangency portfolio, long-only via clipping-and-renormalising.

    Clipping is a crude projection but the alternative (an unconstrained
    solution with large shorts) is worse in practice than a slightly suboptimal
    long-only one.
    """
    cov = _cov(returns, shrinkage)
    mu = np.asarray(expected_returns, dtype=float) - risk_free_rate
    try:
        raw = np.linalg.solve(cov, mu)
    except np.linalg.LinAlgError:
        return inverse_variance_weights(returns)
    if not np.any(raw > 0):
        return min_variance_weights(returns, shrinkage)
    return _normalize(raw)


def risk_parity_weights(
    returns: list[list[float]], iterations: int = 500, tol: float = 1e-9
) -> list[float]:
    """Equal risk contribution, solved by the standard multiplicative fixed point.

    w_i ← w_i * (target_i / RC_i); converges monotonically for positive-definite
    covariance and needs no external solver.
    """
    cov = _cov(returns)
    n = cov.shape[0]
    if n == 1:
        return [1.0]
    w = np.ones(n) / n
    target = 1.0 / n
    for _ in range(iterations):
        port_var = float(w @ cov @ w)
        if port_var <= 0:
            break
        marginal = cov @ w
        contrib = w * marginal / port_var
        step = np.power(target / np.maximum(contrib, 1e-12), 0.5)
        new = w * step
        new = new / new.sum()
        if np.max(np.abs(new - w)) < tol:
            w = new
            break
        w = new
    return _normalize(w)


def hierarchical_risk_parity(returns: list[list[float]]) -> list[float]:
    """López de Prado's HRP: cluster by correlation distance, then bisect.

    Needs no matrix inversion at all, which makes it the most numerically robust
    option when the universe is larger than the return history is long.
    """
    arr = np.asarray(returns, dtype=float)
    n = arr.shape[0]
    if n == 1:
        return [1.0]
    cov = _cov(returns, shrinkage=0.0)
    std = np.sqrt(np.diag(cov))
    corr = cov / np.outer(std, std)
    corr = np.nan_to_num(corr, nan=0.0)
    dist = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, None))

    # Single-linkage quasi-diagonalisation without scipy: repeatedly merge the
    # two closest clusters and keep their concatenated ordering.
    clusters = [[i] for i in range(n)]
    while len(clusters) > 1:
        best = (np.inf, 0, 1)
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                d = min(dist[i, j] for i in clusters[a] for j in clusters[b])
                if d < best[0]:
                    best = (d, a, b)
        _, a, b = best
        clusters[a] = clusters[a] + clusters[b]
        clusters.pop(b)
    order = clusters[0]

    weights = np.ones(n)

    def _cluster_var(items: list[int]) -> float:
        sub = cov[np.ix_(items, items)]
        iv = 1.0 / np.maximum(np.diag(sub), 1e-12)
        iv = iv / iv.sum()
        return float(iv @ sub @ iv)

    stack = [order]
    while stack:
        group = stack.pop()
        if len(group) <= 1:
            continue
        mid = len(group) // 2
        left, right = group[:mid], group[mid:]
        vl, vr = _cluster_var(left), _cluster_var(right)
        alpha = 1.0 - vl / (vl + vr) if (vl + vr) > 0 else 0.5
        for i in left:
            weights[i] *= alpha
        for i in right:
            weights[i] *= 1.0 - alpha
        stack.extend([left, right])
    return _normalize(weights)
