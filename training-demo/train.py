"""Synthetic listwise road-candidate ranking demo. No user data used.
Run: python3 train.py. Requires numpy. Not a screenshot/GPS recognizer.
"""
import json
from pathlib import Path
import numpy as np

OUT = Path(__file__).parent
FEATURES = ['shape_similarity', 'turn_order_similarity', 'endpoint_alignment',
            'landmark_agreement', 'retrace_preservation', 'detour_penalty']

def dataset(seed, count):
    rng = np.random.default_rng(seed)
    x = rng.uniform(0, 1, (count, 5, len(FEATURES)))
    # Artificial label-generating mechanism: deliberately explicit, NOT ground truth.
    utility = x @ np.array([2.5, 2.0, 0.4, 1.4, 1.8, -2.0])
    utility += rng.normal(0, 0.15, utility.shape)
    y = utility.argmax(axis=1)
    return x, y

def evaluate(x, y, w):
    order = np.argsort(-(x @ w), axis=1)
    ranks = np.argmax(order == y[:, None], axis=1) + 1
    return {'top1_accuracy': float(np.mean(ranks == 1)),
            'mean_reciprocal_rank': float(np.mean(1 / ranks))}

def loss(x, y, w):
    z = x @ w
    z -= z.max(axis=1, keepdims=True)
    return float(np.mean(np.log(np.exp(z).sum(axis=1)) - z[np.arange(len(y)), y]))

train_x, train_y = dataset(101, 1200)
val_x, val_y = dataset(202, 300)
test_x, test_y = dataset(303, 300)
w = np.zeros(len(FEATURES))
best_w = w.copy()
best_loss = float('inf')
for epoch in range(1, 1501):
    z = train_x @ w
    z -= z.max(axis=1, keepdims=True)
    probs = np.exp(z)
    probs /= probs.sum(axis=1, keepdims=True)
    probs[np.arange(len(train_y)), train_y] -= 1
    grad = np.einsum('nk,nkf->f', probs, train_x) / len(train_y) + 0.002 * w
    w -= 0.3 * grad
    if epoch % 25 == 0:
        val_loss = loss(val_x, val_y, w)
        if val_loss < best_loss:
            best_loss, best_w = val_loss, w.copy()

report = {
    'synthetic_only': True,
    'warning': 'Labels are generated from a known artificial utility. Scores do not estimate real-world accuracy.',
    'split': {'train_tasks': 1200, 'validation_tasks': 300, 'test_tasks': 300,
              'candidates_per_task': 5, 'unit': 'whole screenshot-like task, not individual candidate'},
    'baseline_endpoint_only': evaluate(test_x, test_y, np.array([0, 0, 1, 0, 0, 0])),
    'trained_ranker': evaluate(test_x, test_y, best_w),
    'best_validation_cross_entropy': best_loss,
    'weights': dict(zip(FEATURES, best_w.tolist()))
}
model = {'model_type': 'linear_listwise_ranker', 'features': FEATURES,
         'weights': best_w.tolist(), 'synthetic_only': True}
(OUT / 'model.json').write_text(json.dumps(model, indent=2))
(OUT / 'metrics.json').write_text(json.dumps(report, indent=2))
# Reload and test that persisted inference matches in-memory inference.
loaded = json.loads((OUT / 'model.json').read_text())
assert np.allclose(test_x @ np.array(loaded['weights']), test_x @ best_w)
# Export a transparent example of the training contract.
example = {'task_id': 'synthetic_0001', 'synthetic': True,
           'candidates': [{'candidate_id': str(i), 'features': dict(zip(FEATURES, v.tolist()))}
                          for i, v in enumerate(train_x[0])],
           'preferred_candidate_id': str(train_y[0])}
(OUT / 'example.json').write_text(json.dumps(example, indent=2))
print(json.dumps(report, indent=2))
print('PASS: model reload and inference consistency')
