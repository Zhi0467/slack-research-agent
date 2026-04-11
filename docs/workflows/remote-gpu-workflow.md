## Remote GPU node constraints
- SSH access is through the configured user; set a local SSH alias (e.g. via `DASHBOARD_GPU_NODE_ALIAS`) for consistency.
- Docker is available but sudo is not.
- Limit Docker bind mounts strictly to `/home/$USER/*` subdirectories; never mount `/` and never mount `/var/run/docker.sock`.
- Never use `--gpus all` with Docker.
- Any GPU-consuming command must run via Slurm and include a realistic explicit memory request (`--mem`).
- Avoid heavy compilation unless resource usage is constrained appropriately.
- Keep runtime secrets on the remote host in `/home/$USER/.env` with restricted file permissions; do not place secret values in tracked repository files.
- repeated `cicc` OOM kills from login-shell workloads marked the GPU node DOWN; do not run heavy compile/inference from SSH/login sessions.
- Required practice: run heavy workloads only via `srun`/`sbatch` with explicit realistic `--mem`.
- Required practice: cap compile parallelism (`MAX_JOBS=8` or `make -j8`) unless explicitly justified otherwise.
- Required practice: after Slurm jobs complete, verify and clean up leftover background processes.
- If the node is occupied which you can check via `squeue`, wait for up to 30 minutes before marking the task waiting human.

## Storage hygiene

Home-directory disk pressure has caused `ENOSPC` (os error 28) failures, dependency-chain stalls, and cross-task interference. These rules are mandatory for every GPU-node session.

### Cache routing

Never let ML framework caches write under `$HOME`. Before any model download, dataset fetch, or inference run, export these environment variables (in your Slurm script or shell session):

```bash
export HF_HOME=/data/users/${USER}/cache/huggingface
export HUGGINGFACE_HUB_CACHE=/data/users/${USER}/cache/huggingface/hub
export TRANSFORMERS_CACHE=/data/users/${USER}/cache/huggingface/transformers
export VLLM_CACHE_ROOT=/data/users/${USER}/cache/vllm
export TORCH_HOME=/data/users/${USER}/cache/torch
export XDG_CACHE_HOME=/data/users/${USER}/cache
```

If `/data/users/${USER}` is unavailable, fall back to `/data/scratch/${USER}/cache`. Never fall back to `$HOME/.cache`.

### Large artifact placement

Store datasets, checkpoints, rollouts, and other large outputs on data volumes — not under home:

| Artifact type | Approved location |
|---|---|
| Downloaded datasets | `/data/users/${USER}/datasets/` or `/data/shared/datasets/` |
| Model checkpoints | `/data/users/${USER}/checkpoints/<project>/` |
| Experiment outputs / rollouts | `/data/users/${USER}/outputs/<project>/` |
| Temporary / scratch files | `/data/scratch/${USER}/` |

Symlink from the project worktree to the data volume if the code expects a local path (e.g., `ln -s /data/users/${USER}/outputs/cot-loop outputs`).

### Preflight checks

Before submitting a Slurm chain or long-running job, verify:

1. **Cache roots** — confirm the cache env vars above are set and point outside `$HOME`.
2. **Home usage** — run `df -h ~` and abort if home filesystem is above 80% capacity.
3. **Stale outputs** — check for leftover outputs from prior runs (`du -sh ~/projects/worktrees/*/outputs/ 2>/dev/null`) and clean them before starting.

Report the preflight results (cache paths, home usage %) in your Slack thread update before the first job submission.

### Cleanup after completion

After experiments finish (or at each milestone transition in long-running loops):

1. **Prune stale outputs** — delete intermediate outputs, rollouts, and probe data that are not on the explicit keep-list for the current task. Only retain final results needed for reporting.
2. **Clean worktrees** — remove GPU-node worktrees that are no longer active (`rm -rf ~/projects/worktrees/<stale-worktree>`). Verify with `du -sh ~/projects/worktrees/*/` before and after.
3. **Verify** — run `df -h ~` after cleanup and report the result in the Slack thread. The human should see visible evidence that cleanup happened.

### During loop mode

When running in `!loop` mode with repeated iterations, apply the cleanup pass at the **start** of each new iteration (not just at the end). This prevents unbounded artifact accumulation between status updates.

## Slurm troubleshooting

### Job stuck PENDING (queue blocker)

When a Slurm job is required as a hard gate (e.g., before merging a PR) but is stuck PENDING:

1. Submit the exact documented E2E command from the project's `scripts/` or launcher docs. Do not substitute undocumented shortcuts.
2. Record the job ID and poll queue state: `squeue -u $USER` and `scontrol show job <JOB_ID>`.
3. Classify the blocker — is it a user-owned job? Can it be preempted? What's the projected release time?
4. Maintain ordering constraints — do not start downstream tasks gated on this job's success.
5. Wait up to 30 minutes, polling every 5 minutes. If the node becomes available, proceed immediately.
6. If still blocked, post one concise Slack update: completed status, queued job ID, blocking job ID + projected release, and a concrete decision request (preempt vs. wait).
7. Set `waiting_human` — the next action depends on a scheduling decision.
8. Never cancel another user's job without explicit human approval.

### CUDA OOM on shared node (GPU memory contention)

When a Slurm job starts but fails with CUDA OOM even though Slurm allocated the requested resources:

1. Capture failure evidence from job logs and `sacct -j <JOB_ID> --format=JobID,State,ExitCode,MaxRSS,Elapsed`.
2. Inspect live GPU state:
   ```bash
   nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv
   nvidia-smi --query-compute-apps=pid,used_gpu_memory,name --format=csv
   ```
3. Classify occupants:
   - **Own stale processes:** Kill them (`kill -9 <PID>`) and retry.
   - **External workloads:** Do not kill — these belong to other users.
4. Do not retry blindly — repeated OOM retries waste cluster cycles.
5. Requeue with a dependency chain if a specific blocker job is identifiable: `sbatch --dependency=afterany:<BLOCKER_JOB_ID> <script.sh>`. Otherwise requeue normally.
6. Post one concise Slack update with root cause, queued fallback job IDs, and expected behavior.
7. Set `waiting_human` if no further executable work exists until resources clear.
8. Always ensure cache env vars point outside `$HOME` (see Cache routing above) — storage pressure often co-occurs with GPU contention.

### Safe shutdown on stop command

When a human sends a stop signal (`!stop`, "stop this", "pause now") while Slurm jobs are active:

1. Confirm stop intent — do not misinterpret status questions as stop signals.
2. Enumerate task-owned jobs (running, pending, dependency-pending): `squeue -u $USER --format="%i %j %T %r %V"`.
3. Cancel all task-owned jobs immediately: `scancel <JOB_ID_1> <JOB_ID_2> ...` — including dependency-pending jobs.
4. Verify final states: `sacct -j <JOB_IDS> --format=JobID,JobName,State,ExitCode,Elapsed --noheader`.
5. Post one concise acknowledgement: stop confirmation, cancellation results, preserved resume checkpoint, `waiting_human` state. Do not post multiple near-duplicate acknowledgements.
6. Note any completed jobs' partial results — they may contain useful output.
7. Set `waiting_human` unless your collaborator explicitly asked for alternate work.
