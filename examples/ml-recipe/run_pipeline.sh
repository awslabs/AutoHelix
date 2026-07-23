#!/bin/bash
# Train + evaluate pipeline.
# Trains a sklearn model from train_config.yaml and evaluates on held-out data.
set -euo pipefail
python train.py && python evaluate.py
