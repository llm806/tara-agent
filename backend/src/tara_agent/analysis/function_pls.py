"""两成分PLS2与相关圆；依据plsdepot算法，省略随机交叉验证及推断。"""

import numpy as np

from tara_agent.analysis.function_study_models import PLSResult


def fit_pls(rows, predictors, responses, *, target, figure):
    variables = predictors + responses
    complete = [
        r
        for r in rows
        if all(isinstance(r.get(k), (int, float)) and np.isfinite(r[k]) for k in variables)
    ]
    base = dict(
        target=target,
        figure=figure,
        sample_count=len(complete),
        excluded_rows=len(rows) - len(complete),
        input_rows=rows,
    )
    if len(complete) < 4:
        return PLSResult(**base, status="blocked", reason="完整观测不足4组，无法稳定提取两个成分")
    values = np.array([[r[k] for k in variables] for r in complete], dtype=float)
    sd = values.std(axis=0, ddof=1)
    constants = [k for k, s in zip(variables, sd, strict=True) if s == 0]
    # 不静默删除论文指定变量；不同变量集合不可冒充同一PLS模型。
    if constants:
        return PLSResult(
            **base,
            status="blocked",
            reason="存在常量变量，标准化未定义",
            constant_variables=constants,
        )
    scaled = (values - values.mean(axis=0)) / sd
    x = scaled[:, : len(predictors)].copy()
    y = scaled[:, len(predictors) :].copy()
    if np.linalg.matrix_rank(x) < 2:
        return PLSResult(**base, status="blocked", reason="环境矩阵秩不足2")
    scores = []
    for _ in range(2):
        u = y[:, 0].copy()
        old = np.ones(x.shape[1])
        for _iteration in range(100):
            if u @ u <= np.finfo(float).eps:
                return PLSResult(**base, status="blocked", reason="响应残差信号不足，成分未定义")
            w = x.T @ u / (u @ u)
            norm = np.linalg.norm(w)
            if norm <= np.finfo(float).eps:
                return PLSResult(**base, status="blocked", reason="环境与响应无可提取协变信号")
            w /= norm
            t = x @ w
            if t @ t <= np.finfo(float).eps:
                return PLSResult(**base, status="blocked", reason="成分方差未定义")
            c = y.T @ t / (t @ t)
            if c @ c <= np.finfo(float).eps:
                return PLSResult(**base, status="blocked", reason="响应载荷未定义")
            u = y @ c / (c @ c)
            if (w - old) @ (w - old) < 1e-6:
                break
            old = w.copy()
        else:
            return PLSResult(**base, status="blocked", reason="PLS迭代100次未收敛")
        p = x.T @ t / (t @ t)
        x -= np.outer(t, p)
        y -= np.outer(t, c)
        scores.append(t)
    t = np.column_stack(scores)
    corr = np.corrcoef(values.T, t.T)[: len(variables), -2:]
    if not np.isfinite(corr).all():
        return PLSResult(**base, status="blocked", reason="相关坐标存在未定义值")
    explained = []
    for i in range(2):
        explained.append(
            {
                "component": float(i + 1),
                "x_fraction": float(np.mean(corr[: len(predictors), i] ** 2)),
                "y_fraction": float(np.mean(corr[len(predictors) :, i] ** 2)),
            }
        )
    return PLSResult(
        **base,
        status="completed",
        coordinates=[
            {
                "variable": k,
                "role": "environment" if k in predictors else "response",
                "component_1": float(np.clip(corr[i, 0], -1, 1)),
                "component_2": float(np.clip(corr[i, 1], -1, 1)),
            }
            for i, k in enumerate(variables)
        ],
        scores=[
            {
                "sampling_group": str(r["sampling_group"]),
                "component_1": float(t[i, 0]),
                "component_2": float(t[i, 1]),
            }
            for i, r in enumerate(complete)
        ],
        explained_variance=explained,
    )
