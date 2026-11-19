Lower val_bpb of a small GPT trained from scratch on one GPU in a fixed time.

This is Karpathy's autoresearch (upstream 228791f): train.py trains a GPT on ClimbMix
text with the tokenizer prepare.py built. Edit only train.py: model, optimizer,
schedule, batch, size, anything in it. Changes to other files are discarded.

How an attempt is scored: run RUN_EXPERIMENT (about 7 minutes). It scores the current
train.py with ar/train_and_score.sh and prints its output, then the metrics, `val_bpb: X`
first. It runs at most MAX_EXPERIMENTS experiments in total; after that it refuses.
- train.py runs twice at once, with different seeds (AR_SEED), each in a sandbox
  (ar/sandbox.sh): one H200, no network, the training shards and tokenizer read-only
  where prepare.py looks for them (not the validation shard), writes only to its
  working folder. Don't install packages.
- It is killed 380 s after launch (TIME_LIMIT in ar/train_and_score.sh), with imports,
  torch.compile and the export included. prepare.TIME_BUDGET (300 s of steps) is a
  timer in your own code: when to stop is yours, but a run still going at the limit
  scores nothing.
- Before exiting, train.py calls ar.export.save(model): forward(idx) -> logits, int64
  [16, 2048] -> float [16, 2048, 8192], under bf16 autocast, exported with
  torch.export (fixed shapes; aten ops and FA3's forward only). See ar/export.py.
- ar/score.py, in a separate process, computes the cross-entropy from those logits on
  the validation shard (shard_06542, about 21M tokens, as upstream's evaluate_bpb)
  and rejects models that aren't causal or mix batch rows. val_bpb is the mean of the
  two. The loss train.py computes is your own business.
- Gate: ar/static_check.py (under 64 KB, allowed imports, no large literals). The
  final result is scored again on a held-out test shard after the run.

Check a change with `bash ar/smoke.sh` (about 70 s, same sandbox and GPU, 30 s of
steps): it says whether train.py runs and exports, nothing about val_bpb. Run full
trainings only through RUN_EXPERIMENT.
All else equal, prefer the simpler change.
