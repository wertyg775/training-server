import argparse
import json
import os
import time
from pathlib import Path

import torch
from torch import nn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=os.environ.get("GPU_JOB_OUTPUT_DIR")
    )
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--pause", type=float, default=2.0)
    args = parser.parse_args()
    if args.epochs < 1 or args.pause < 0:
        parser.error("epochs must be positive and pause must be nonnegative")
    if args.output is None:
        parser.error("Pass --output or submit through gpu-run")
    args.output.mkdir(parents=True, exist_ok=True)
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required; CPU fallback is disabled")

    torch.set_num_threads(2)
    torch.manual_seed(42)
    device = torch.device("cuda:0")
    print(
        f"device={torch.cuda.get_device_name(0)} torch={torch.__version__}", flush=True
    )
    generator = torch.Generator().manual_seed(42)
    features = torch.randn(4096, 64, generator=generator)
    teacher = torch.randn(64, 4, generator=generator)
    labels = (features @ teacher).argmax(dim=1)
    x, y = features.to(device), labels.to(device)
    model = nn.Sequential(nn.Linear(64, 128), nn.ReLU(), nn.Linear(128, 4)).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.003)
    loss_fn = nn.CrossEntropyLoss()

    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        for start in range(0, len(x), 128):
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(model(x[start : start + 128]), y[start : start + 128])
            if not torch.isfinite(loss).item():
                raise RuntimeError("Non-finite loss")
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        model.eval()
        with torch.no_grad():
            accuracy = (model(x).argmax(dim=1) == y).float().mean().item()
        metrics = {"epoch": epoch, "loss": total_loss / 32, "accuracy": accuracy}
        print(json.dumps(metrics), flush=True)
        with (args.output / "metrics.jsonl").open("a") as handle:
            handle.write(json.dumps(metrics) + "\n")
        checkpoint = {
            "epoch": epoch,
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "metrics": metrics,
        }
        temporary = args.output / "checkpoint.pt.tmp"
        torch.save(checkpoint, temporary)
        temporary.replace(args.output / "checkpoint.pt")
        time.sleep(args.pause)

    print(
        f"COMPLETED epochs={args.epochs} checkpoint={args.output / 'checkpoint.pt'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
