"""可独立检验的生态统计；随机算法固定种子并返回实际有效样本数。"""

import numpy as np
from scipy.optimize import isotonic_regression
from scipy.spatial.distance import pdist, squareform


def adjust_bh(values):
    output = [None] * len(values)
    indices = [i for i, p in enumerate(values) if p is not None]
    indices.sort(key=lambda i: values[i])
    last = 1.0
    for rank in range(len(indices), 0, -1):
        i = indices[rank - 1]
        last = min(last, values[i] * len(indices) / rank)
        output[i] = float(last)
    return output


def association(rows, x, y, *, permutations=999, seed=42):
    pairs = [(r.get(x), r.get(y)) for r in rows]
    pairs = [(a, b) for a, b in pairs if finite(a) and finite(b)]
    base = dict(
        variable=x,
        response=y,
        sample_count=len(pairs),
        excluded_rows=len(rows) - len(pairs),
        rho=None,
        p_value=None,
        p_adjusted=None,
        method="Spearman_two_sided_permutation",
    )
    if len(pairs) < 3:
        return {**base, "status": "blocked", "reason": "成对完整观测不足3组"}
    a, b = np.asarray(pairs, dtype=float).T
    if np.ptp(a) == 0 or np.ptp(b) == 0:
        return {**base, "status": "blocked", "reason": "常量变量的相关性未定义"}
    from scipy.stats import rankdata

    a, b = rankdata(a), rankdata(b)
    a, b = a - a.mean(), b - b.mean()
    scale = np.linalg.norm(a) * np.linalg.norm(b)
    rho = float(np.clip(a @ b / scale, -1, 1))
    rng = np.random.default_rng(seed)
    extreme = sum(
        abs(a @ rng.permutation(b) / scale) >= abs(rho) - 1e-12 for _ in range(permutations)
    )
    return {
        **base,
        "rho": rho,
        "p_value": (extreme + 1) / (permutations + 1),
        "permutations": permutations,
        "seed": seed,
        "status": "completed",
        "reason": None,
    }


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and np.isfinite(value)


def nmds(matrix, *, seed=42, starts=20, max_iterations=1000, tolerance=1e-7):
    """Bray–Curtis + 单调SMACOF，强相等秩处理；与vegan属于同类方法而非逐坐标复刻。"""
    matrix = np.asarray(matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] < 4 or not np.isfinite(matrix).all():
        raise ValueError("NMDS需要至少4组有限观测")
    if (matrix < 0).any() or (matrix.sum(axis=1) <= 0).any():
        raise ValueError("NMDS不接受负值、缺失或全零分布")
    delta = pdist(matrix, metric="braycurtis")
    if np.ptp(delta) == 0:
        raise ValueError("Bray–Curtis距离无秩变化，NMDS未定义")
    n = len(matrix)
    order = np.argsort(delta, kind="stable")
    _, inverse, weights = np.unique(delta[order], return_inverse=True, return_counts=True)
    rng = np.random.default_rng(seed)
    best = None
    for start in range(starts):
        coords = rng.normal(size=(n, 2))
        coords -= coords.mean(axis=0)
        previous = np.inf
        converged = False
        for _iteration in range(max_iterations):
            distances = pdist(coords)
            means = np.bincount(inverse, weights=distances[order]) / weights
            fitted = isotonic_regression(means, weights=weights).x[inverse]
            disparities = np.empty_like(delta)
            disparities[order] = fitted
            norm = np.linalg.norm(disparities)
            if norm <= np.finfo(float).eps:
                break
            disparities *= np.sqrt(len(delta)) / norm
            ratio = disparities / np.maximum(distances, 1e-12)
            b = -squareform(ratio)
            np.fill_diagonal(b, -b.sum(axis=1))
            updated = b @ coords / n
            new_distances = pdist(updated)
            stress = float(
                np.sqrt(np.sum((new_distances - disparities) ** 2) / np.sum(new_distances**2))
            )
            coords = updated
            if abs(previous - stress) < tolerance:
                converged = True
                break
            previous = stress
        if norm > np.finfo(float).eps and (best is None or stress < best["stress"]):
            # 转动和翻转不影响距离；主轴旋转仅稳定图形方向。
            u, s, _ = np.linalg.svd(coords - coords.mean(axis=0), full_matrices=False)
            best = dict(
                coordinates=u[:, :2] * s[:2],
                stress=stress,
                converged=converged,
                iterations=_iteration + 1,
                best_start=start + 1,
            )
    if best is None:
        raise ValueError("NMDS无可用解")
    best.update(
        seed=seed,
        starts=starts,
        max_iterations=max_iterations,
        tolerance=tolerance,
        method="Bray_Curtis_monotonic_SMACOF_strong_ties_Stress1_no_autotransform",
    )
    best["distances"] = squareform(delta)
    return best


def envfit(coords, rows, variables, *, permutations=999, seed=42):
    """环境向量最小二乘拟合；对每个变量的完整行做双坐标置换检验。"""
    results = []
    for variable in variables:
        selected = [i for i, row in enumerate(rows) if finite(row.get(variable))]
        base = dict(
            variable=variable,
            sample_count=len(selected),
            excluded_rows=len(rows) - len(selected),
            r_squared=None,
            p_value=None,
            p_adjusted=None,
            axis_1=None,
            axis_2=None,
        )
        if len(selected) < 4:
            results.append({**base, "status": "blocked", "reason": "完整环境观测不足4组"})
            continue
        x = np.asarray(coords)[selected]
        x = x - x.mean(axis=0)
        y = np.array([rows[i][variable] for i in selected], dtype=float)
        y -= y.mean()
        if np.linalg.matrix_rank(x) < 2 or y @ y == 0:
            results.append({**base, "status": "blocked", "reason": "环境常量或排序矩阵秩不足"})
            continue
        fit = np.linalg.lstsq(x, y, rcond=None)[0]
        r2 = float(np.clip(np.sum((x @ fit) ** 2) / (y @ y), 0, 1))
        rng = np.random.default_rng(seed)
        extreme = 0
        for _ in range(permutations):
            yp = rng.permutation(y)
            fitted = x @ np.linalg.lstsq(x, yp, rcond=None)[0]
            extreme += np.sum(fitted**2) / (yp @ yp) >= r2 - 1e-12
        direction = fit / np.linalg.norm(fit) if np.linalg.norm(fit) else np.zeros(2)
        results.append(
            {
                **base,
                "status": "completed",
                "reason": None,
                "r_squared": r2,
                "p_value": (extreme + 1) / (permutations + 1),
                "axis_1": float(direction[0] * np.sqrt(r2)),
                "axis_2": float(direction[1] * np.sqrt(r2)),
                "permutations": permutations,
                "seed": seed,
            }
        )
    for row, adjusted in zip(results, adjust_bh([r["p_value"] for r in results]), strict=True):
        row["p_adjusted"] = adjusted
    return results
