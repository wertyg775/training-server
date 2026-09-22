"""Small CPU regression workload for testing generated uv environments."""

import argparse
import json
import os
from pathlib import Path

import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=3)
    args = parser.parse_args()
    if args.epochs < 1:
        parser.error("epochs must be positive")
    x = np.arange(1, 6, dtype=float)
    y = 2 * x
    weight = 0.0
    for _ in range(args.epochs):
        weight -= 0.01 * float(np.mean(2 * x * (weight * x - y)))
    result = {
        "epochs": args.epochs,
        "weight": weight,
        "loss": float(np.mean((weight * x - y) ** 2)),
    }
    print(json.dumps(result))
    if output := os.environ.get("GPU_JOB_OUTPUT_DIR"):
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "metrics.json").write_text(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
