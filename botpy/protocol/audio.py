import asyncio
import shutil
import struct
from pathlib import Path
from typing import Optional, Union

from .media_utils import clean_extension, is_audio_file


QQ_NATIVE_VOICE_EXTENSIONS = {".silk", ".slk", ".amr", ".wav", ".mp3"}


def pcm_to_wav(
    pcm_data: bytes,
    sample_rate: int,
    channels: int = 1,
    bits_per_sample: int = 16,
) -> bytes:
    """将 PCM s16le 等原始样本封装为标准 WAV。"""

    if sample_rate <= 0 or channels <= 0 or bits_per_sample <= 0:
        raise ValueError("audio format values must be positive")
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    header = b"".join(
        (
            b"RIFF",
            struct.pack("<I", 36 + len(pcm_data)),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample),
            b"data",
            struct.pack("<I", len(pcm_data)),
        )
    )
    return header + pcm_data


def strip_amr_header(data: bytes) -> bytes:
    return data[6:] if data.startswith(b"#!AMR\n") else data


def should_transcode_voice(source: str, mime_type: Optional[str] = None) -> bool:
    if clean_extension(source) in QQ_NATIVE_VOICE_EXTENSIONS:
        return False
    if mime_type and mime_type.lower() in {
        "audio/silk",
        "audio/amr",
        "audio/wav",
        "audio/wave",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp3",
    }:
        return False
    return is_audio_file(source, mime_type)


def detect_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


async def ffmpeg_to_pcm(
    input_path: Union[str, Path],
    sample_rate: int = 24_000,
    *,
    ffmpeg: Optional[str] = None,
) -> bytes:
    executable = ffmpeg or detect_ffmpeg()
    if executable is None:
        raise RuntimeError("ffmpeg is not installed or not available on PATH")
    process = await asyncio.create_subprocess_exec(
        executable,
        "-i",
        str(input_path),
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        "-v",
        "error",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace').strip()}")
    return stdout
