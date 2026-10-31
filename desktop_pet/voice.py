"""Local voice layer for the desktop pet: record, transcribe, speak.

This module is deliberately independent of the agent.  Speech-to-text turns a
microphone clip into text that is sent through the *existing* agent; text-to-
speech only reads the agent's real reply.  There is no second reasoning system.

Backends
--------
* Recording: ``sounddevice`` (PortAudio), 16 kHz mono PCM.
* STT: Gemini (reuses the key in ``mcp_server/config.json``) by default, then a
  local ``faster-whisper`` model, then an optional OpenAI-compatible
  ``/audio/transcriptions`` endpoint.
* TTS: Edge TTS (Arabic, online) with a Windows SAPI fallback (offline).

Edge TTS is free and needs no API key.  SAPI is offline but the default Windows
installation only has English voices, so Arabic quality is lower.  The engine
choice and the fallback are reported to the caller; nothing is silently
pretended to have worked.

The local Whisper model is downloaded once into ``desktop_pet/cache/whisper``
and then runs offline on the CPU.  It is a fallback, not a second agent: the
transcript is sent through the same existing agent.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import io
import logging
import os
import re
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

import httpx
import numpy as np

log = logging.getLogger("othvm.pet.voice")

try:
    import sounddevice as sd
    import soundfile as sf
except Exception:  # pragma: no cover - optional at import time
    sd = None
    sf = None

try:
    import pythoncom
    import win32com.client
except Exception:  # pragma: no cover - optional
    pythoncom = None
    win32com = None

VoiceCallback = Callable[[str, dict], None]


class VoiceError(RuntimeError):
    """Raised when a voice backend is unavailable or fails."""


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
class AudioRecorder:
    """Push-to-talk recorder returning a WAV byte string."""

    def __init__(self, samplerate: int = 16000, channels: int = 1,
                 device: Any = None):
        self.samplerate = int(samplerate)
        self.channels = int(channels)
        self.device = device or None
        self._stream = None
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._level = 0.0
        self._frame_count = 0

    @property
    def available(self) -> bool:
        return sd is not None and sf is not None

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def level(self) -> float:
        with self._lock:
            return self._level

    @property
    def duration(self) -> float:
        with self._lock:
            return self._frame_count / float(self.samplerate)

    def start(self) -> None:
        if not self.available:
            raise VoiceError("sounddevice/soundfile غير مثبتين — الصوت غير متاح.")
        if self._stream is not None:
            return
        with self._lock:
            self._frames = []
            self._frame_count = 0
            self._level = 0.0
        try:
            stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="int16",
                device=self.device,
                callback=self._callback,
                blocksize=0,
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001
            raise VoiceError(f"تعذّر فتح الميكروفون: {exc}") from exc
        self._stream = stream

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            log.debug("recorder status: %s", status)
        block = indata.copy()
        with self._lock:
            self._frames.append(block)
            self._frame_count += int(frames)
            try:
                peak = float(np.abs(block).max()) / 32768.0
                rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) / 32768.0
                self._level = max(peak, rms * 2.0)
            except Exception:
                self._level = 0.0

    def stop(self) -> bytes:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        with self._lock:
            frames = self._frames
            self._frames = []
            self._frame_count = 0
            self._level = 0.0
        if not frames:
            return b""
        audio = np.concatenate(frames, axis=0)
        buffer = io.BytesIO()
        sf.write(buffer, audio, self.samplerate, format="WAV", subtype="PCM_16")
        return buffer.getvalue()

    def cancel(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._lock:
            self._frames = []
            self._frame_count = 0
            self._level = 0.0


def list_input_devices() -> list[dict]:
    if sd is None:
        return []
    out = []
    try:
        default = sd.default.device[0]
    except Exception:
        default = -1
    try:
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_input_channels") or 0) <= 0:
                continue
            out.append({
                "index": index,
                "name": str(device.get("name") or ""),
                "channels": int(device.get("max_input_channels") or 0),
                "default_samplerate": device.get("default_samplerate"),
                "default": index == default,
            })
    except Exception:
        return []
    return out


# ---------------------------------------------------------------------------
# Speech to text
# ---------------------------------------------------------------------------
_LANGUAGE_NAMES = {
    "ar": "Arabic (Egyptian)",
    "ar-eg": "Arabic (Egyptian)",
    "en": "English",
    "en-us": "English",
}

# Loaded Whisper models are expensive to build, so keep them for the process
# lifetime.  Keyed by model/device/compute type/download root.
_LOCAL_MODELS: dict[str, Any] = {}
_LOCAL_MODEL_LOCK = threading.Lock()


class SpeechToText:
    """Transcribe a WAV clip with an explicit backend chain.

    Default chain: Gemini (cloud) -> local faster-whisper -> optional
    OpenAI-compatible endpoint.  A backend that raises is skipped and the next
    one is tried.  A backend that succeeds with an empty transcript ends the
    chain, so silence is not re-interpreted by a second model.
    """

    def __init__(self, config: dict, gemini_key: str):
        self.config = dict(config or {})
        self.gemini_key = str(gemini_key or "").strip()
        self.engine = str(self.config.get("engine") or "gemini").lower()
        self.model = str(self.config.get("model") or "gemini-3.8-flash")
        self.language = str(self.config.get("language") or "ar")
        self.openai_base_url = str(self.config.get("openai_base_url") or "").rstrip("/")
        self.openai_model = str(self.config.get("openai_model") or "whisper-1")
        self.gemini_timeout = float(self.config.get("gemini_timeout_seconds") or 15)
        self.gemini_retries = max(0, int(self.config.get("gemini_retries") or 1))
        self.local_fallback = self.config.get("local_fallback", True) is not False
        self.local_model = str(self.config.get("local_model") or "base")
        self.local_device = str(self.config.get("local_device") or "cpu")
        self.local_compute_type = str(self.config.get("local_compute_type") or "int8")
        self.local_beam_size = int(self.config.get("local_beam_size") or 1)
        self.local_vad = self.config.get("local_vad", True) is not False
        self.local_timeout = float(self.config.get("local_timeout_seconds") or 120)
        self.local_load_timeout = float(
            self.config.get("local_load_timeout_seconds") or 900)
        self.local_preload = self.config.get("local_preload", True) is not False
        root = str(self.config.get("local_download_root") or "").strip()
        self.local_download_root = Path(root) if root else (
            Path(__file__).resolve().parent / "cache" / "whisper")

    @property
    def local_available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    def backends(self) -> list[str]:
        """Backends that are actually configured and usable, in order."""
        primary = self.engine if self.engine in ("gemini", "openai", "local") else "gemini"
        order = [primary]
        for name in ("gemini", "local", "openai"):
            if name not in order:
                order.append(name)
        out: list[str] = []
        for name in order:
            if name == "gemini" and not self.gemini_key:
                continue
            if name == "openai" and not self.openai_base_url:
                continue
            if name == "local":
                if not self.local_available:
                    continue
                if not (self.local_fallback or primary == "local"):
                    continue
            out.append(name)
        return out

    @property
    def available(self) -> bool:
        return bool(self.backends())

    def describe(self) -> str:
        names = {
            "gemini": "Gemini",
            "local": f"محلي ({self.local_model})",
            "openai": "OpenAI",
        }
        return " → ".join(names.get(name, name) for name in self.backends()) or "غير متاح"

    def preload(self) -> bool:
        """Warm the local model in the background so a fallback is fast.

        The first call may download the model; it never blocks the UI.
        """
        if "local" not in self.backends() or not self.local_preload:
            return False

        def worker() -> None:
            try:
                self._run_with_timeout(
                    self._local_model, self.local_load_timeout,
                    "تحميل نموذج Whisper المحلي")
                log.info("local Whisper model ready: %s", self.local_model)
            except Exception as exc:  # noqa: BLE001
                log.warning("local Whisper preload failed: %s", exc)

        threading.Thread(target=worker, name="othvm-pet-whisper-preload",
                         daemon=True).start()
        return True

    def transcribe(self, audio: bytes, mime_type: str = "audio/wav") -> dict:
        audio = bytes(audio or b"")
        if not audio:
            return {"text": "", "engine": self.engine, "empty": True}
        started = time.monotonic()
        errors: list[str] = []
        for name in self.backends():
            try:
                if name == "gemini":
                    result = self._gemini(audio, mime_type)
                elif name == "local":
                    result = self._local(audio)
                else:
                    result = self._openai(audio, mime_type)
                result["seconds"] = round(time.monotonic() - started, 2)
                if errors:
                    result["fallback_from"] = errors
                return result
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                log.warning("STT backend %s failed: %s", name, exc)
        raise VoiceError("; ".join(errors)[:600] or "لا يوجد محرك STT متاح")

    @staticmethod
    def _run_with_timeout(fn: Callable[[], Any], timeout: float, label: str) -> Any:
        """Run a blocking backend call with a bounded wait.

        Python cannot cancel the worker thread, so a timed-out call is left to
        finish in the background (daemon).  The caller gets an honest error.
        """
        box: dict[str, Any] = {}
        done = threading.Event()

        def runner() -> None:
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=runner, name="othvm-pet-whisper",
                         daemon=True).start()
        if not done.wait(timeout):
            raise VoiceError(f"{label}: تجاوز المهلة ({timeout:.0f} ثانية)")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _local_model(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            raise VoiceError(
                "faster-whisper غير مثبت — ثبّت desktop_pet/requirements.txt") from exc
        key = "|".join((self.local_model, self.local_device,
                        self.local_compute_type, str(self.local_download_root)))
        with _LOCAL_MODEL_LOCK:
            cached = _LOCAL_MODELS.get(key)
            if cached is not None:
                return cached
            self.local_download_root.mkdir(parents=True, exist_ok=True)
            log.info("loading local Whisper model %s (%s/%s)",
                     self.local_model, self.local_device, self.local_compute_type)
            model = WhisperModel(
                self.local_model,
                device=self.local_device,
                compute_type=self.local_compute_type,
                download_root=str(self.local_download_root),
            )
            _LOCAL_MODELS[key] = model
            return model

    @staticmethod
    def _decode_wav(audio: bytes) -> np.ndarray:
        """Decode WAV bytes to 16 kHz mono float32.

        faster-whisper's own decoder currently calls a PyAV API that is not
        available in every av release, so decode the (already 16 kHz) recorder
        output with soundfile and hand the model an array.
        """
        if sf is None:
            raise VoiceError("soundfile غير مثبت — تعذّر فك ترميز الصوت محليًا.")
        try:
            data, rate = sf.read(io.BytesIO(audio), dtype="float32", always_2d=True)
        except Exception as exc:  # noqa: BLE001
            raise VoiceError(f"تعذّر فك ترميز WAV: {exc}") from exc
        if data.size == 0:
            return np.zeros(0, dtype=np.float32)
        if data.shape[1] > 1:
            data = data.mean(axis=1)
        else:
            data = data[:, 0]
        data = np.asarray(data, dtype=np.float32)
        if int(rate) != 16000:
            duration = data.shape[0] / float(rate)
            target = max(0, int(round(duration * 16000)))
            if target == 0:
                return np.zeros(0, dtype=np.float32)
            old_x = np.linspace(0.0, duration, num=data.shape[0], endpoint=False)
            new_x = np.linspace(0.0, duration, num=target, endpoint=False)
            data = np.interp(new_x, old_x, data).astype(np.float32)
        return np.ascontiguousarray(data, dtype=np.float32)

    def _local(self, audio: bytes) -> dict:
        model = self._run_with_timeout(
            self._local_model, self.local_load_timeout,
            "تحميل نموذج Whisper المحلي")
        language = self.language.split("-")[0] if self.language else None
        samples = self._decode_wav(audio)
        if samples.size == 0:
            return {"text": "", "engine": "local", "model": self.local_model,
                    "language": language or "", "empty": True}
        started = time.monotonic()

        def decode() -> tuple[str, Any]:
            segments, info = model.transcribe(
                samples,
                language=language,
                beam_size=self.local_beam_size,
                vad_filter=self.local_vad,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            return text, info

        text, info = self._run_with_timeout(
            decode, self.local_timeout, "تفريغ الصوت محليًا")
        probability = getattr(info, "language_probability", None)
        return {
            "text": _clean_transcript(text),
            "engine": "local",
            "model": self.local_model,
            "language": str(getattr(info, "language", "") or language or ""),
            "language_probability": (
                round(float(probability), 3) if probability is not None else None),
            "decode_seconds": round(time.monotonic() - started, 2),
        }

    def _prompt(self) -> str:
        language = _LANGUAGE_NAMES.get(self.language.lower(), self.language)
        return (
            "Transcribe this audio exactly as spoken. "
            f"Language hint: {language}. "
            "Return only the transcript, without commentary, labels, or quotes. "
            "If there is no speech, return an empty string."
        )

    def _gemini(self, audio: bytes, mime_type: str) -> dict:
        if not self.gemini_key:
            raise VoiceError("gemini_api_key غير موجود في mcp_server/config.json")
        model = self.model.strip()
        if model.startswith("models/"):
            model = model[7:]
        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": self._prompt()},
                    {"inlineData": {
                        "mimeType": mime_type or "audio/wav",
                        "data": base64.b64encode(audio).decode("ascii"),
                    }},
                ],
            }],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 2048},
        }
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        last_error = ""
        for attempt in range(self.gemini_retries + 1):
            try:
                with httpx.Client(timeout=self.gemini_timeout) as client:
                    response = client.post(url, params={"key": self.gemini_key},
                                           json=payload)
            except httpx.TimeoutException as exc:
                # A hanging cloud call must not delay the local fallback.
                raise VoiceError(
                    f"Gemini STT timeout after {self.gemini_timeout:.0f}s") from exc
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.gemini_retries:
                    time.sleep(1.0)
                continue
            if (response.status_code in (429, 500, 502, 503, 504)
                    and attempt < self.gemini_retries):
                last_error = f"HTTP {response.status_code}"
                time.sleep(1.5)
                continue
            if response.status_code != 200:
                raise VoiceError(
                    f"Gemini STT HTTP {response.status_code}: {response.text[:400]}")
            data = response.json()
            feedback = data.get("promptFeedback") or {}
            if feedback.get("blockReason"):
                raise VoiceError(f"Gemini blocked the audio: {feedback['blockReason']}")
            candidates = data.get("candidates") or []
            if not candidates:
                raise VoiceError("Gemini returned no candidates")
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(str(part.get("text") or "") for part in parts).strip()
            return {"text": _clean_transcript(text), "engine": "gemini",
                    "model": model, "language": self.language}
        raise VoiceError(f"Gemini STT failed after retries: {last_error}")

    def _openai(self, audio: bytes, mime_type: str) -> dict:
        if not self.openai_base_url:
            raise VoiceError("openai_base_url غير مضبوط")
        url = self.openai_base_url
        if not url.endswith("/audio/transcriptions"):
            url = url.rstrip("/") + "/audio/transcriptions"
        headers = {}
        # Optional OpenAI-compatible keys come from the environment, never from
        # the pet's own config file.
        key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
        if key:
            headers["Authorization"] = "Bearer " + key
        files = {"file": ("speech.wav", audio, mime_type or "audio/wav")}
        data = {"model": self.openai_model}
        if self.language:
            data["language"] = self.language.split("-")[0]
        with httpx.Client(timeout=180) as client:
            response = client.post(url, headers=headers, files=files, data=data)
        if response.status_code != 200:
            raise VoiceError(
                f"STT HTTP {response.status_code}: {response.text[:400]}")
        payload = response.json()
        text = str((payload or {}).get("text") or "").strip()
        return {"text": _clean_transcript(text), "engine": "openai",
                "model": self.openai_model, "language": self.language}


def _clean_transcript(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^(transcript|النص|التفريغ)\s*[:：]\s*", "", text,
                  flags=re.IGNORECASE)
    return text.strip().strip('"').strip("'").strip()


# ---------------------------------------------------------------------------
# Text to speech
# ---------------------------------------------------------------------------
def clean_for_speech(text: str, max_chars: int = 600) -> str:
    """Remove markup that should not be read aloud and bound the length."""
    value = str(text or "")
    if not value.strip():
        return ""
    value = re.sub(r"```.*?```", " ", value, flags=re.S)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[*_#>|]+", " ", value)
    value = re.sub(r"^\s*[-•]\s*", "", value, flags=re.M)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_chars:
        return value
    cut = value[:max_chars]
    for mark in ("۔", ".", "؟", "!", "،", ";", ":"):
        index = cut.rfind(mark)
        if index > max_chars * 0.55:
            return cut[:index + 1].strip()
    return cut.rstrip() + "…"


def _short_path(path: Path) -> str:
    try:
        size = 4096
        buffer = ctypes.create_unicode_buffer(size)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                               wintypes.DWORD]
        kernel32.GetShortPathNameW.restype = wintypes.DWORD
        written = kernel32.GetShortPathNameW(str(path), buffer, size)
        if written and written < size:
            return buffer.value
    except Exception:
        pass
    return str(path)


def _mci(command: str) -> int:
    winmm = ctypes.windll.winmm
    winmm.mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                     wintypes.UINT, wintypes.HWND]
    winmm.mciSendStringW.restype = ctypes.c_uint
    return int(winmm.mciSendStringW(command, None, 0, None))


def _mci_error(code: int) -> str:
    try:
        winmm = ctypes.windll.winmm
        buffer = ctypes.create_unicode_buffer(512)
        winmm.mciGetErrorStringW.argtypes = [ctypes.c_uint, wintypes.LPWSTR,
                                             wintypes.UINT]
        winmm.mciGetErrorStringW.restype = wintypes.BOOL
        if winmm.mciGetErrorStringW(code, buffer, 512):
            return buffer.value
    except Exception:
        pass
    return f"MCI error {code}"


class SpeechEngine:
    """Asynchronous TTS with an explicit engine/fallback report."""

    def __init__(self, config: dict, cache_dir: Path,
                 on_event: VoiceCallback | None = None):
        self.config = dict(config or {})
        self.cache_dir = Path(cache_dir) / "tts"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._on_event = on_event or (lambda *_: None)
        self._lock = threading.RLock()
        self._generation = 0
        self._speaking = False
        self._thread: threading.Thread | None = None
        self._mci_alias: str | None = None
        self._sapi_voice = None
        self.engine = str(self.config.get("engine") or "edge").lower()
        self.voice = str(self.config.get("voice") or "ar-EG-SalmaNeural")
        self.rate = str(self.config.get("rate") or "+0%")
        self.volume = str(self.config.get("volume") or "+0%")
        self.max_chars = int(self.config.get("max_chars") or 600)
        self.sapi_voice_name = str(self.config.get("sapi_voice") or "")
        self.allow_sapi_fallback = self.config.get("allow_sapi_fallback", True) is not False
        self.last_engine = ""

    @property
    def speaking(self) -> bool:
        with self._lock:
            return self._speaking

    @property
    def sapi_available(self) -> bool:
        return win32com is not None

    @property
    def edge_available(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except Exception:
            return False

    def describe(self) -> str:
        engines = []
        if self.edge_available:
            engines.append(f"Edge ({self.voice})")
        if self.sapi_available:
            engines.append("SAPI")
        return " / ".join(engines) or "غير متاح"

    def speak(self, text: str) -> bool:
        value = clean_for_speech(text, self.max_chars)
        if not value:
            return False
        self.stop()
        with self._lock:
            self._generation += 1
            generation = self._generation
        self._thread = threading.Thread(
            target=self._run, args=(value, generation),
            name="othvm-pet-tts", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            was_speaking = self._speaking
            self._speaking = False
            alias = self._mci_alias
            sapi = self._sapi_voice
        if alias:
            try:
                _mci(f"stop {alias}")
            except Exception:
                pass
        if sapi is not None:
            try:
                sapi.Speak("", 2)  # SVSFPurgeBeforeSpeak
            except Exception:
                pass
        if was_speaking:
            self._emit("speaking_end", {"engine": self.last_engine, "stopped": True})

    def _emit(self, kind: str, data: dict) -> None:
        try:
            self._on_event(kind, data)
        except Exception:
            log.exception("voice callback failed")

    def _run(self, text: str, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._speaking = True
        self._emit("speaking_start", {"engine": self.engine, "text_chars": len(text)})
        engine_used = ""
        try:
            if self.engine == "edge" and self.edge_available:
                try:
                    path = self._edge_synthesize(text, generation)
                    if path and generation == self._generation:
                        self._play_mci(path, generation)
                        engine_used = "edge"
                except Exception as exc:  # noqa: BLE001
                    log.warning("edge TTS failed: %s", exc)
                    if not self.allow_sapi_fallback:
                        raise
            if not engine_used and generation == self._generation:
                if self.sapi_available:
                    self._sapi_speak(text, generation)
                    engine_used = "sapi"
                else:
                    raise VoiceError("لا يوجد محرك TTS متاح (Edge فشل وSAPI غير مثبت).")
            self.last_engine = engine_used
        except Exception as exc:  # noqa: BLE001
            log.warning("TTS failed: %s", exc)
            self._emit("tts_error", {"message": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            with self._lock:
                if generation == self._generation:
                    self._speaking = False
                    self._emit("speaking_end", {"engine": engine_used})

    def _cache_path(self, text: str) -> Path:
        digest = hashlib.sha256(
            f"{self.voice}|{self.rate}|{self.volume}|{text}".encode("utf-8")
        ).hexdigest()[:32]
        return self.cache_dir / f"{digest}.mp3"

    def _edge_synthesize(self, text: str, generation: int) -> Path | None:
        path = self._cache_path(text)
        if path.exists() and path.stat().st_size > 1000:
            return path
        import edge_tts

        async def run() -> None:
            communicate = edge_tts.Communicate(
                text, self.voice, rate=self.rate, volume=self.volume)
            await communicate.save(str(path))

        asyncio.run(run())
        if generation != self._generation:
            return None
        if not path.exists() or path.stat().st_size < 1000:
            raise VoiceError("Edge TTS produced no audio")
        return path

    def _play_mci(self, path: Path, generation: int) -> None:
        alias = f"othvmtts{os.getpid()}{generation}"
        with self._lock:
            self._mci_alias = alias
        opened = False
        try:
            code = _mci(f'open "{_short_path(path)}" type mpegvideo alias {alias}')
            if code:
                raise VoiceError(_mci_error(code))
            opened = True
            code = _mci(f"play {alias} wait")
            if code and generation == self._generation:
                raise VoiceError(_mci_error(code))
        finally:
            if opened:
                _mci(f"close {alias}")
            with self._lock:
                if self._mci_alias == alias:
                    self._mci_alias = None

    def _sapi_speak(self, text: str, generation: int) -> None:
        if win32com is None:
            raise VoiceError("pywin32 غير مثبت — SAPI غير متاح.")
        if pythoncom is not None:
            pythoncom.CoInitialize()
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            if self.sapi_voice_name:
                try:
                    tokens = voice.GetVoices()
                    for index in range(tokens.Count):
                        token = tokens.Item(index)
                        if self.sapi_voice_name.lower() in str(token.GetDescription()).lower():
                            voice.Voice = token
                            break
                except Exception:
                    pass
            with self._lock:
                self._sapi_voice = voice
            voice.Speak(text, 1)  # SVSFlagsAsync
            while generation == self._generation:
                try:
                    if not voice.Status.Running:
                        break
                except Exception:
                    break
                time.sleep(0.1)
            if generation != self._generation:
                try:
                    voice.Speak("", 2)
                except Exception:
                    pass
        finally:
            with self._lock:
                self._sapi_voice = None
            if pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass


def list_sapi_voices() -> list[str]:
    if win32com is None:
        return []
    if pythoncom is not None:
        pythoncom.CoInitialize()
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        tokens = voice.GetVoices()
        return [str(tokens.Item(i).GetDescription()) for i in range(tokens.Count)]
    except Exception:
        return []
    finally:
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
