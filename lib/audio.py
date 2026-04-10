"""Audio extraction and conversion for Rocksmith CDLC."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# iOS doesn't support subprocess (no fork/exec) — detect and skip CLI paths
_IS_IOS = sys.platform == "ios" or os.environ.get("SLOPSMITH_ANDROID") == "1" and False  # Android uses subprocess
_IS_MOBILE = sys.platform == "ios"


def _ffmpeg_cmd():
    """Find ffmpeg executable. On Android, it's bundled as libffmpeg.so."""
    if _IS_MOBILE:
        return None  # No subprocess on iOS
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    return shutil.which("ffmpeg")


def _vgmstream_cmd():
    """Find vgmstream-cli executable. On Android, it's bundled as libvgmstream.so."""
    if _IS_MOBILE:
        return None  # No subprocess on iOS — use ctypes instead
    env_path = os.environ.get("VGMSTREAM_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    return shutil.which("vgmstream-cli")


def find_wem_files(extracted_dir: str) -> list[str]:
    """Find WEM audio files, sorted largest first (full song before preview)."""
    wem_files = list(Path(extracted_dir).rglob("*.wem"))
    wem_files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return [str(f) for f in wem_files]


def convert_wem(wem_path: str, output_base: str) -> str:
    """
    Convert a WEM file to a playable format.
    Returns path to the converted audio file.
    """
    # Try vgmstream-cli → WAV via stdout → ffmpeg → MP3 via stdout
    # On Android, native binaries can't write to app dirs (SELinux W^X).
    # Solution: pipe output through stdout so Python handles all file I/O.
    vgmstream = _vgmstream_cmd()
    if vgmstream:
        # vgmstream -o - outputs WAV to stdout
        wav = output_base + ".wav"
        r = subprocess.run(
            [vgmstream, "-o", "-", wem_path], capture_output=True
        )
        if r.returncode == 0 and len(r.stdout) > 100:
            # Write WAV from stdout
            with open(wav, 'wb') as f:
                f.write(r.stdout)

            ffmpeg = _ffmpeg_cmd()
            if ffmpeg:
                # ffmpeg reads WAV, outputs MP3 to stdout via pipe:1
                mp3 = output_base + ".mp3"
                r2 = subprocess.run(
                    [ffmpeg, "-y", "-i", wav, "-b:a", "192k", "-f", "mp3", "pipe:1"],
                    capture_output=True,
                )
                if r2.returncode == 0 and len(r2.stdout) > 100:
                    with open(mp3, 'wb') as f:
                        f.write(r2.stdout)
                    os.remove(wav)
                    return mp3
            return wav

    # Try ffmpeg directly (some builds handle Wwise)
    ffmpeg = _ffmpeg_cmd()
    if ffmpeg:
        # Output MP3 to stdout
        mp3 = output_base + ".mp3"
        r = subprocess.run(
            [ffmpeg, "-y", "-i", wem_path, "-b:a", "192k", "-f", "mp3", "pipe:1"],
            capture_output=True,
        )
        if r.returncode == 0 and len(r.stdout) > 100:
            with open(mp3, 'wb') as f:
                f.write(r.stdout)
            return mp3

        # Try WAV to stdout
        wav = output_base + ".wav"
        r = subprocess.run(
            [ffmpeg, "-y", "-i", wem_path, "-f", "wav", "pipe:1"],
            capture_output=True,
        )
        if r.returncode == 0 and len(r.stdout) > 100:
            with open(wav, 'wb') as f:
                f.write(r.stdout)
            return wav

    # Try ww2ogg
    if shutil.which("ww2ogg"):
        ogg = output_base + ".ogg"
        r = subprocess.run(
            ["ww2ogg", wem_path, "-o", ogg], capture_output=True
        )
        if r.returncode == 0 and os.path.exists(ogg) and os.path.getsize(ogg) > 0:
            return ogg

    # ctypes vgmstream fallback (iOS — dylib loaded via ctypes)
    try:
        from vgmstream_decode import decode_wem_to_wav
        wav = output_base + ".wav"
        if decode_wem_to_wav(wem_path, wav):
            return wav
    except Exception as e:
        print(f"vgmstream ctypes decode failed: {e}")

    # Pure Python fallback (last resort)
    try:
        from wem_decode import convert_wem_to_ogg
        ogg = output_base + ".ogg"
        if convert_wem_to_ogg(wem_path, ogg):
            return ogg
    except Exception as e:
        print(f"Python WEM decode failed: {e}")

    raise RuntimeError(
        "No WEM audio decoder found. Install vgmstream-cli:\n"
        "  Manjaro/Arch:  yay -S vgmstream-cli-bin\n"
        "  Or build from: github.com/vgmstream/vgmstream"
    )
