"""
Modal.com GPU Kokoro TTS.

Deploy once:
    modal deploy modal_tts.py

Then app.py calls kokoro_tts.remote(text, voice) to get back WAV bytes.
Runs on a T4 GPU — ~50x faster than CPU, ~$0.10-0.30 per full book.
"""
import modal

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("espeak-ng", "ffmpeg")
    .pip_install(
        "kokoro>=0.9.2",
        "soundfile>=0.12.1",
        "numpy>=1.24.0",
        "torch>=2.0.0",
    )
)

app = modal.App("bookwave-tts", image=image)

# Cache the Kokoro model between calls so we don't re-download every invocation.
_pipelines = {}


@app.function(
    gpu="T4",
    timeout=1800,
    scaledown_window=300,
)
def kokoro_tts(text: str, voice: str) -> bytes:
    """Generate MP3 audio bytes for `text` using Kokoro voice `voice`."""
    import io
    import subprocess
    import tempfile
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline

    lang = "b" if voice.startswith("b") else "a"
    if lang not in _pipelines:
        _pipelines[lang] = KPipeline(lang_code=lang)
    pipe = _pipelines[lang]

    chunks = []
    for _, _, audio in pipe(text, voice=voice, speed=1.0):
        chunks.append(audio)
    if not chunks:
        raise RuntimeError("Kokoro returned no audio")

    # Write WAV to temp file then convert to MP3 with ffmpeg
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_f:
        wav_path = wav_f.name
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mp3_f:
        mp3_path = mp3_f.name

    sf.write(wav_path, np.concatenate(chunks), 24000)
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, "-codec:a", "libmp3lame", "-qscale:a", "3", mp3_path],
        check=True, capture_output=True
    )
    with open(mp3_path, "rb") as f:
        return f.read()


@app.local_entrypoint()
def test():
    """Smoke test: `modal run modal_tts.py` → writes test.wav locally."""
    data = kokoro_tts.remote(
        "Hello from Modal. This is a test of the Kokoro text to speech system running on a GPU.",
        "af_heart",
    )
    with open("test.wav", "wb") as f:
        f.write(data)
    print(f"Wrote test.wav — {len(data):,} bytes")
