# Running the M2 campaign on a rented GPU

Which host to pick: **Choosing a host**, at the bottom. Kaggle and Colab both
work; neither is the fastest way to finish.

## Kaggle

The archive's state lives in Convex + R2, so a transcription worker is stateless:
any machine with the `.env` can pick up where the last one died (§4.2/§4.3). A
Kaggle notebook is such a machine, with two constraints — 12 h per session and
~30 GPU-hours/week — so the campaign is a sequence of sessions, not one run.

No code change is needed. What changes is the batch size (small, so a killed
session loses minutes of GPU, not hours) and the exit signal (`SIGINT` before
Kaggle's own kill, so the stage lock is released and a `pipelineRuns` row is
written).

## One-time setup

1. **Kaggle account**: verify the phone number — without it a notebook has no
   internet, and this worker talks to Convex, R2 and Hugging Face.
2. **Secrets** (Add-ons → Secrets), both from this repo:
   - `ARCHIVE_ENV_B64` — the whole environment as one line:
     ```sh
     cat .env .env.local | base64 | tr -d '\n' | pbcopy
     ```
     It needs `HF_TOKEN`, the five `R2_*` values and `CONVEX_URL`. Not the
     Telegram session — M2 never touches Telegram.
   - `GITHUB_TOKEN` — a read-only PAT for this private repo.
3. **Notebook settings**: Accelerator `GPU T4 x2` (only one GPU is used; T4 has
   fp16 tensor cores, P100 does not), Internet `On`, Persistence off.

## The notebook

Cell 1 — setup (~6 min: torch + the CUDA wheels, then the 2B model on first use):

```python
import base64, os, pathlib, subprocess, sys
from kaggle_secrets import UserSecretsClient

s = UserSecretsClient()
ROOT = pathlib.Path("/kaggle/temp/telegram-archive")   # /kaggle/temp: scratch,
os.environ["HF_HOME"] = "/kaggle/temp/hf"              # not committed as output

if not ROOT.exists():
    token = s.get_secret("GITHUB_TOKEN")
    subprocess.run(
        ["git", "clone", "--depth", "1",
         f"https://{token}@github.com/haithamassoli-plus-connect/telegram-archive.git",
         str(ROOT)], check=True)

(ROOT / ".env").write_bytes(base64.b64decode(s.get_secret("ARCHIVE_ENV_B64")))
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"], check=True)
subprocess.run(["uv", "sync", "--extra", "gpu", "--frozen"], cwd=ROOT, check=True)
```

Cell 2 — the run:

```python
!cd /kaggle/temp/telegram-archive && timeout -s INT 11h uv run archive transcribe --batch-size 25
```

`timeout -s INT` sends the same signal Ctrl-C does, so the run releases the
Convex stage lock and records itself instead of being shot at hour 12.

## Running it

- First session: append `--limit 10` and watch coverage move, then read the
  log's minutes-per-file to get the real RTF on a T4. The M0 benchmark
  (23.5x on MPS) is the floor, not the estimate: 2,926.6 audio hours at 60x is
  ~49 GPU-hours ≈ two weeks of the free weekly quota.
- After that, always run it as **Save Version → Save & Run All (Commit)**. An
  interactive session dies when the browser does; a commit runs headless to the
  12 h limit.
- Between sessions there is nothing to resume by hand. Rows left `processing`
  by a kill are claimable again after 5 minutes, and every artifact already in
  R2 is promoted to `done` without inference on the next scan (§4.3).
- Never run Kaggle and the local Mac at the same time. It is safe — the second
  one is refused by the stage lock — but it wastes a session.
- Stop when `archive transcribe` reports coverage ≥99%, then run
  `archive reconcile-artifacts` once locally.

## Two things to know

- **dtype is not part of `configHash`.** Kaggle's T4 resolves `auto` to fp16,
  the Mac to bf16. Both are legal under the pin, but the text can differ in the
  last decimals; finishing the campaign on one platform keeps the corpus
  homogeneous.
- **`--batch-size 25`, not the default 500.** Uploads happen after a whole
  batch comes off the GPU, so a hard kill mid-batch throws that batch away.
  25 files is a few minutes; 500 is most of a session.

## Google Colab

Same worker, three differences: secrets come from `google.colab.userdata`, the
scratch root is `/content`, and there is no headless commit — a free or Pro
session needs the tab open, and only Pro+ has background execution (up to 24 h
with the browser closed). Runtime -> Change runtime type -> T4 (free) or L4 (Pro).

Cell 1:

```python
import base64, os, pathlib, subprocess, sys
from google.colab import userdata

ROOT = pathlib.Path("/content/telegram-archive")
os.environ["HF_HOME"] = "/content/hf"

if not ROOT.exists():
    token = userdata.get("GITHUB_TOKEN")
    subprocess.run(
        ["git", "clone", "--depth", "1",
         f"https://{token}@github.com/haithamassoli-plus-connect/telegram-archive.git",
         str(ROOT)], check=True)

(ROOT / ".env").write_bytes(base64.b64decode(userdata.get("ARCHIVE_ENV_B64")))
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"], check=True)
subprocess.run(["uv", "sync", "--extra", "gpu", "--frozen"], cwd=ROOT, check=True)
```

Cell 2 — the `timeout` matches the tier's real session length, not Kaggle's 11 h:

```python
!cd /content/telegram-archive && timeout -s INT 3h uv run archive transcribe --batch-size 25
```

The two secrets go in the key sidebar, one per name, with notebook access on.

### On a Google AI Pro subscription

The plan carries Colab Pro's benefits: ~100 compute units a month, the L4 and
A100 runtimes, and 24 h of maximum runtime. Three things follow.

* **Pick L4, not A100.** Units are the scarce resource, not seconds, and the
  A100 burns roughly 3x the units per hour for well under 3x the throughput.
  T4 and L4 land close per unit; L4 finishes sooner.
* **Check Runtime for a "Background execution" toggle.** With it, the tab can
  close. Without it (it has historically been a Pro+ feature), the browser must
  stay connected for the whole run — a laptop that sleeps ends the session.
* **100 units will not cover 2,927 audio hours.** Measure instead of guessing:
  after the first batch, `audio_hours_done / units_spent` (the balance is in the
  Colab resources panel) x 2,927 is the real bill. Expect the monthly allowance
  to buy somewhere around two thirds of the campaign, which makes Colab a
  contributor to the campaign rather than the whole of it.


## Choosing a host

The campaign is ~2,927 audio hours. Everything else follows from one property:
**the worker is stateless and crash-safe**, so an interruptible GPU costs
nothing but the batch it dies in — which is why renting a cheap spot instance
beats babysitting a free one.

| Host | Cost | Wall clock | Why |
|---|---|---|---|
| Vast.ai / RunPod, one RTX 4090 or L40S | ~$0.3-0.7/h, **$10-40 total** | 1-2 days | No session cap, `tmux` + one command, spot pricing is safe here |
| The Mac that is already set up | free | ~5.2 days continuous | Zero setup — it is the measured 23.5x baseline |
| Kaggle | free, ~30 GPU-h/week | 2-4 weeks | Headless 12 h commits, but the weekly quota is the ceiling |
| Colab Pro (incl. Google AI Pro) | already paid | ~1 day of L4 | ~100 units/month buys most of the campaign, not all of it |
| Colab free | free | never | 90-minute idle kill, tab must stay open, no scheduling |

**Recommended: rent one GPU for a day.** For the price of a lunch the campaign
is over, `--batch-size` goes back up to a few hundred, and nobody clicks "Save &
Run All" every morning for three weeks. Kaggle is the right answer only if the
budget is strictly zero; Colab is the wrong shape for a multi-day batch job at
any tier below Pro+.

On a rented box there is no notebook at all:

```sh
git clone --depth 1 https://<token>@github.com/haithamassoli-plus-connect/telegram-archive.git
cd telegram-archive && printf '%s' '<ARCHIVE_ENV_B64>' | base64 -d > .env
pip install uv && uv sync --extra gpu --frozen
tmux new -s asr 'uv run archive transcribe --batch-size 200'
```
