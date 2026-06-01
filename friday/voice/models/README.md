# Friday Wake-Word Models

Custom-trained [openWakeWord](https://github.com/dscripka/openWakeWord) models that detect Friday's wake phrases. These models are loaded at runtime by `voice/wakeword.py`.

## Interim state (2026-05-31)

**Custom training is deferred.** Smoke-testing the training pipeline showed it takes ~3.5 hours on this M1 for a tiny 50-sample run, which projects to **10–30 hours per phrase** for a real model with the full corpus. That's not viable on the current hardware.

For now, `voice/wakeword.py` aliases the phrase "Hey Friday" to openWakeWord's bundled **`hey_jarvis_v0.1`** model. **In practice this means you must say _"Hey Jarvis"_ aloud to trigger Friday** — the model only fires on jarvis phonemes, not friday phonemes. The other phrases ("Friday you up", "Friday status", "Friday") have no model and will silently never fire.

When custom-trained `.onnx` files land in this directory by phrase slug (`hey_friday.onnx`, `friday_you_up.onnx`, etc.), `voice/wakeword.py` picks them up automatically with no code change. Until then: train remotely on a cloud GPU, or live with "Hey Jarvis".

## What ships here

| Phrase | File | Status |
|---|---|---|
| "Hey Friday" | `hey_jarvis.onnx` (interim alias) | **Fires on "Hey Jarvis" only.** Replace with a real `hey_friday.onnx` when trained. |
| "Friday you up" | (none) | No model. Won't fire. |
| "Friday status" | (none) | No model. Won't fire. |
| "Friday" (solo) | (none) | No model. Gated behind `voice.solo_trigger_enabled` anyway. |

Models are stored as **`.onnx`**, not `.tflite`. openWakeWord's runtime accepts both, and on a Mac with `onnxruntime` installed there is no measurable inference-speed difference. The TFLite conversion path requires `tensorflow-cpu==2.8.1` which has no macOS-arm64 wheel, so we skip it.

## Retraining from scratch

The full pipeline lives in `voice/training/`. Everything below assumes the working directory is `voice/`.

### One-time setup

1. Install system deps (already present on Keller's machine):
   ```sh
   brew install portaudio ffmpeg
   ```

2. Python 3.12 is required (NOT 3.14 — most ML wheels lag). The training venv is `voice/.venv`:
   ```sh
   /usr/local/bin/python3.12 -m venv voice/.venv
   ```

3. Clone the openwakeword trainer source (it's not a pip-installable wheel for our purposes — we need the `train.py` CLI):
   ```sh
   cd voice && git clone --depth=1 https://github.com/dscripka/openWakeWord.git trainer_src
   ```

4. Install the training stack into the venv:
   ```sh
   .venv/bin/pip install \
     piper-sample-generator piper-tts \
     torchinfo torchmetrics audiomentations torch_audiomentations \
     speechbrain mutagen pronouncing acoustics \
     pyyaml requests tqdm scipy onnx datasets torchcodec
   .venv/bin/pip install -e ./trainer_src
   ```

5. Download openWakeWord's pre-trained feature extractors:
   ```sh
   D=trainer_src/openwakeword/resources/models
   mkdir -p $D
   for f in embedding_model.onnx embedding_model.tflite melspectrogram.onnx melspectrogram.tflite; do
     curl -fsSL -o $D/$f \
       "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/$f"
   done
   ```

### Why we patch the venv

The trainer was last tuned for Linux + scipy 1.x + torchaudio 2.0. On modern macOS we need three compat shims, all already applied to this venv:

- **`acoustics/directivity.py`** — `scipy.special.sph_harm` renamed to `sph_harm_y` (with swapped args) in scipy >=1.15, and `scipy.interpolate.interp2d` was removed in 1.14. Both are patched in-place.
- **`_friday_compat.pth` + `_friday_compat.py`** in `site-packages/` — restore `torchaudio.info` using `soundfile`. Removed in torchaudio 2.10+.
- **`trainer_src/openwakeword/train.py:872`** — force `num_workers=0` on the training DataLoader. The default uses worker processes that can't pickle the `mmap_batch_generator` lambda on Python 3.12+ macOS (spawn start method).

If you wipe the venv, re-apply all three. The patches are self-contained — no recompilation required.

### Why we ship our own `generate_samples.py` shim

The openwakeword trainer does `from generate_samples import generate_samples` to produce synthetic positive/negative wavs. It expects the **old** `rhasspy/piper-sample-generator` API (git clone, no PyPI release).

The current PyPI package `piper-sample-generator 3.2.0` has a different API AND depends on `piper_train.vits` which is source-only in `rhasspy/piper` and requires `piper-phonemize~=1.1.0` (no macOS wheels, must build from source against espeak-ng) plus `torch<2` (conflicts with our torch 2.12) plus a Cython extension.

Rather than dive into that swamp, `voice/training/piper_shim/generate_samples.py` exposes the trainer's expected `generate_samples()` signature, implemented directly on top of `piper-tts` (which bundles espeak-ng and just works on macOS).

The shim:
- Discovers `.onnx` voices from `voice/training/voices/` (override with `FRIDAY_PIPER_VOICES_DIR`)
- Cycles through the input phrase list to fill `max_samples`
- Picks random length/noise/noise_w per sample for diversity
- **Resamples 22050 Hz → 16 kHz** (Piper voices output at 22050; openwakeword trains on 16 kHz — mismatch silently kills accuracy)
- Honors `file_names=` if the trainer passes pre-generated UUIDs

We lose the SLERP speaker-embedding mixing that the original `rhasspy/piper-sample-generator` does. Compensate with more downloaded voices (see below) and higher `augmentation_rounds`.

### Voice pool

Download multiple Piper voices into `voice/training/voices/`. More voices = better speaker generalization. Suggested minimum (6 voices, ~400 MB):

```sh
mkdir -p voice/training/voices && cd voice/training/voices
for v in en_US-lessac-medium en_US-ryan-medium en_US-libritts_r-medium en_US-hfc_male-medium en_US-hfc_female-medium en_US-kathleen-low; do
  region=$(echo $v | cut -d'-' -f1)
  speaker=$(echo $v | cut -d'-' -f2)
  quality=$(echo $v | cut -d'-' -f3)
  curl -fsSL -o $v.onnx \
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/$region/$speaker/$quality/$v.onnx?download=true"
  curl -fsSL -o $v.onnx.json \
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/$region/$speaker/$quality/$v.onnx.json?download=true"
done
```

### Background-noise corpus

The trainer's `--augment_clips` stage mixes background audio + room impulse responses into the generated wavs. For real (not smoke) training:

- **Room impulse responses**: davidscripka/MIT_environmental_impulse_responses on HuggingFace. ~50 MB. Drop wav files into `voice/training/work/mit_rirs/`.
- **Background audio**: AudioSet `bal_train09.tar` (~5 GB) extracted and converted to 16 kHz wavs in `voice/training/work/audioset_16k/`. Optional: FMA small for music.
- **False-positive validation features**: `validation_set_features.npy` from davidscripka/openwakeword_features (~180 MB).
- **Training feature data**: `openwakeword_features_ACAV100M_2000_hrs_16bit.npy` from the same repo (~17 GB). This is the heavy one — pre-computed features over 2000 hours of speech that serve as the negative class.

Total real-training corpus: **~25 GB**.

### Training a single model

1. Copy `voice/training/smoke.yml` to `voice/training/<phrase>.yml`. Adjust:
   - `model_name`: e.g. `hey_friday`
   - `target_phrase`: e.g. `["hey friday"]`
   - `n_samples`: 20,000+ for real quality (config default 10,000)
   - `n_samples_val`: 2,000
   - `steps`: 50,000
   - `augmentation_rounds`: 2 or 3 (offset the SLERP loss)
   - `feature_data_files.ACAV100M`: path to the 17 GB feature file
   - `false_positive_validation_data_path`: path to validation_set_features.npy
   - `background_paths`: `[audioset_16k, fma]` (whatever you downloaded)
   - `rir_paths`: `[mit_rirs]`

2. Run the three stages, in order:
   ```sh
   VENV=voice/.venv/bin/python
   T=voice/trainer_src/openwakeword/train.py
   $VENV $T --training_config voice/training/hey_friday.yml --generate_clips
   $VENV $T --training_config voice/training/hey_friday.yml --augment_clips
   $VENV $T --training_config voice/training/hey_friday.yml --train_model
   ```

3. The trainer writes `voice/training/work/<model_name>_out/<model_name>/<model_name>.onnx`. Copy it to `voice/models/<phrase>.onnx`.

Expected wall-clock on M1 CPU (no MPS — the trainer hard-codes `cuda or cpu`):
- `--generate_clips`: ~3–10 min depending on n_samples
- `--augment_clips`: ~5–15 min
- `--train_model`: ~1–4 hours for 50k steps

### Solo "Friday" recipe

Single-token wake words are structurally hard. Use a separate config with:
- `custom_negative_phrases`: phrases sharing phonemes — `["fry day", "Frida", "Friday", "fried egg", "wide eye", "Tuesday", "Sunday", ...]` (the trainer auto-generates more, but seed it)
- `n_samples` ≥ 30,000
- Higher `target_false_positives_per_hour` tolerance (~1.0) because solo phrases naturally trip more
- After training, gate behind `voice.solo_trigger_enabled: false` in `friday_config.yaml` until you've validated the FP rate against your daily life.

### Testing a model

```sh
voice/.venv/bin/python trainer_src/examples/detect_from_microphone.py \
  --model_path voice/models/hey_friday.onnx
```

Speak. Watch the score. Anything > 0.5 is a trigger. Try at varying distances + with background music — that's what live use looks like.

## Smoke-test config

`voice/training/smoke.yml` is the minimal config used to validate the toolchain after any dependency change. ~50 positive + 50 negative samples, 1000 training steps, ~5 min wall clock. Run all three stages with it — if any stage fails, the trainer is broken and a real model run will waste hours.

## Known limitations

- **No SLERP mixing** — fewer effective speakers than the original openwakeword pipeline. Mitigation: more downloaded voices + `augmentation_rounds >= 2`.
- **No MPS** — train.py hard-codes `'cuda:0' if cuda else 'cpu'`. Training runs at CPU speed.
- **No automatic `.tflite` export** — the trainer's conversion path needs tensorflow-cpu 2.8.1 which doesn't build on Python 3.12 / M1. We ship `.onnx` instead.
