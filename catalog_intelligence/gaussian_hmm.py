"""
Minimal diagonal-covariance Gaussian HMM (Baum-Welch fit + Viterbi decode).

hmmlearn does not currently ship a wheel for Python 3.14 and this machine has no
C++ build toolchain, so this reimplements the small slice of hmmlearn's
GaussianHMM behavior this project needs: fit() via EM and decode() via Viterbi,
using scaled forward-backward for numerical stability.
"""

from __future__ import annotations

import numpy as np

_EPS = 1e-6


class GaussianHMM:
    def __init__(self, n_states: int = 3, n_iter: int = 200, tol: float = 1e-4, random_state: int = 0):
        self.n_states = n_states
        self.n_iter = n_iter
        self.tol = tol
        self.rng = np.random.default_rng(random_state)
        self.means_: np.ndarray | None = None
        self.covars_: np.ndarray | None = None
        self.transmat_: np.ndarray | None = None
        self.startprob_: np.ndarray | None = None

    def _emission_probs(self, X: np.ndarray) -> np.ndarray:
        n_samples, n_features = X.shape
        probs = np.ones((n_samples, self.n_states))
        for s in range(self.n_states):
            var = np.maximum(self.covars_[s], _EPS)
            diff = X - self.means_[s]
            log_p = -0.5 * (np.log(2 * np.pi * var) + (diff**2) / var).sum(axis=1)
            probs[:, s] = np.exp(log_p)
        return np.maximum(probs, _EPS)

    def _init_params(self, X: np.ndarray) -> None:
        n_samples, n_features = X.shape
        order = np.argsort(X[:, 0])
        chunks = np.array_split(order, self.n_states)
        self.means_ = np.array([X[c].mean(axis=0) for c in chunks])
        self.covars_ = np.array([X[c].var(axis=0) + _EPS for c in chunks])
        self.transmat_ = np.full((self.n_states, self.n_states), 1.0 / self.n_states)
        self.startprob_ = np.full(self.n_states, 1.0 / self.n_states)

    def fit(self, sequences: list[np.ndarray]) -> "GaussianHMM":
        X_all = np.vstack(sequences)
        self._init_params(X_all)
        prev_ll = -np.inf

        for _ in range(self.n_iter):
            total_ll = 0.0
            gamma_sum = np.zeros(self.n_states)
            gamma_x_sum = np.zeros((self.n_states, X_all.shape[1]))
            gamma_xx_sum = np.zeros((self.n_states, X_all.shape[1]))
            xi_sum = np.zeros((self.n_states, self.n_states))
            start_sum = np.zeros(self.n_states)
            gamma0_denom = 0.0

            for X in sequences:
                B = self._emission_probs(X)
                n_samples = X.shape[0]

                # scaled forward pass
                alpha = np.zeros((n_samples, self.n_states))
                c = np.zeros(n_samples)
                alpha[0] = self.startprob_ * B[0]
                c[0] = alpha[0].sum()
                alpha[0] /= c[0]
                for t in range(1, n_samples):
                    alpha[t] = (alpha[t - 1] @ self.transmat_) * B[t]
                    c[t] = alpha[t].sum()
                    alpha[t] /= max(c[t], _EPS)

                # scaled backward pass
                beta = np.zeros((n_samples, self.n_states))
                beta[-1] = 1.0
                for t in range(n_samples - 2, -1, -1):
                    beta[t] = (self.transmat_ @ (B[t + 1] * beta[t + 1])) / max(c[t + 1], _EPS)

                total_ll += np.log(np.maximum(c, _EPS)).sum()

                gamma = alpha * beta
                gamma /= np.maximum(gamma.sum(axis=1, keepdims=True), _EPS)

                xi = np.zeros((self.n_states, self.n_states))
                for t in range(n_samples - 1):
                    num = (alpha[t][:, None] * self.transmat_) * (B[t + 1] * beta[t + 1])[None, :]
                    xi += num / max(num.sum(), _EPS)

                start_sum += gamma[0]
                gamma0_denom += 1
                gamma_sum += gamma.sum(axis=0)
                gamma_x_sum += gamma.T @ X
                gamma_xx_sum += gamma.T @ (X**2)
                xi_sum += xi

            self.startprob_ = start_sum / max(gamma0_denom, 1)
            self.transmat_ = xi_sum / np.maximum(xi_sum.sum(axis=1, keepdims=True), _EPS)
            self.means_ = gamma_x_sum / np.maximum(gamma_sum[:, None], _EPS)
            self.covars_ = np.maximum(gamma_xx_sum / np.maximum(gamma_sum[:, None], _EPS) - self.means_**2, _EPS)

            if abs(total_ll - prev_ll) < self.tol * max(1.0, abs(prev_ll)):
                break
            prev_ll = total_ll

        return self

    def decode(self, X: np.ndarray) -> np.ndarray:
        """Viterbi most-likely state path for one sequence."""
        n_samples = X.shape[0]
        B = self._emission_probs(X)
        log_start = np.log(np.maximum(self.startprob_, _EPS))
        log_trans = np.log(np.maximum(self.transmat_, _EPS))
        log_B = np.log(B)

        delta = np.zeros((n_samples, self.n_states))
        psi = np.zeros((n_samples, self.n_states), dtype=int)
        delta[0] = log_start + log_B[0]
        for t in range(1, n_samples):
            scores = delta[t - 1][:, None] + log_trans
            psi[t] = scores.argmax(axis=0)
            delta[t] = scores.max(axis=0) + log_B[t]

        path = np.zeros(n_samples, dtype=int)
        path[-1] = delta[-1].argmax()
        for t in range(n_samples - 2, -1, -1):
            path[t] = psi[t + 1, path[t + 1]]
        return path
