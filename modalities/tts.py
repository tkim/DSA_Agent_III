"""
Text-to-speech via Lemonade 11.0 (OpenMOSS backend).

Powers the CLI `/speak` command: turns the last answer into audio and plays it.

Endpoint note: Lemonade 11.0 added TTS, but the exact HTTP path can vary by
build. This module tries the OpenAI-style audio endpoint first
(`{base}/v1/audio/speech`) and honors an explicit override via LEMONADE_TTS_URL.
Confirm the path in Phase-0 Gate C and, if needed, set LEMONADE_TTS_URL (and
TTS_MODEL / TTS_VOICE) in `.env`. Everything here is best-effort: a failure
returns an error string and never interrupts the REPL.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from core import config


def _endpoints() -> list[str]:
    if config.LEMONADE_TTS_URL:
        return [config.LEMONADE_TTS_URL]
    root = config.native_base_url()
    # Try the OpenAI-compatible audio path, then a Lemonade-native fallback.
    return [f"{root}/v1/audio/speech", f"{root}/api/v1/audio/speech"]


def synthesize(text: str, out_path: str | None = None) -> str:
    """
    Synthesize `text` to an audio file and return its path.
    Raises RuntimeError with a readable message if every endpoint fails.
    """
    if not text.strip():
        raise RuntimeError("Nothing to speak (empty text).")

    payload = {"input": text[:4000], "response_format": "wav"}
    if config.TTS_MODEL:
        payload["model"] = config.TTS_MODEL
    if config.TTS_VOICE:
        payload["voice"] = config.TTS_VOICE

    out_path = out_path or os.path.join(
        tempfile.gettempdir(), "dsa_iii_speak.wav"
    )

    errors: list[str] = []
    for url in _endpoints():
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {config.LEMONADE_API_KEY}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S) as resp:
                data = resp.read()
            with open(out_path, "wb") as fh:
                fh.write(data)
            return out_path
        except (urllib.error.URLError, OSError) as exc:  # noqa: PERF203
            errors.append(f"{url}: {exc}")

    raise RuntimeError(
        "TTS request failed on all endpoints. Set LEMONADE_TTS_URL in .env after "
        "confirming the path (Phase-0 Gate C).\n  " + "\n  ".join(errors)
    )


def play(path: str) -> None:
    """Play an audio file using the OS default player (best-effort)."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", path])
        else:
            subprocess.Popen(["aplay", path])
    except Exception:  # noqa: BLE001 - playback is a convenience, not required
        pass


def speak(text: str) -> str:
    """Synthesize and play; returns the audio file path."""
    path = synthesize(text)
    play(path)
    return path
