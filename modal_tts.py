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
    # Keep the container warm for 5 min after last call so consecutive
    # chapters re-use the loaded model.
    scaledown_window=300,
)
def kokoro_tts(text: str, voice: str) -> bytes:
    """Generate WAV audio bytes for `text` using Kokoro voice `voice`."""
    import io
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

    buf = io.BytesIO()
    sf.write(buf, np.concatenate(chunks), 24000, format="WAV")
    return buf.getvalue()


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
