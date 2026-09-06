"""Deterministic Chinese chapter ordering and safe audiobook manifests."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import PurePosixPath

DIGITS = dict(zip("零〇一二三四五六七八九两", [0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2]))
UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
AUDIO = {".mp3", ".m4b", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}
EBOOKS = {".epub", ".pdf", ".mobi", ".azw3", ".cbz", ".cbr"}
IMAGES = {".jpg", ".jpeg", ".png", ".webp"}


def chinese_number(value: str) -> int:
    if value.isdecimal():
        return int(value)
    if not value or any(c not in DIGITS and c not in UNITS for c in value):
        raise ValueError(f"Invalid chapter number: {value}")
    if not any(c in UNITS for c in value):
        return int("".join(str(DIGITS[c]) for c in value))
    total = section = digit = 0
    for char in value:
        if char in DIGITS:
            digit = DIGITS[char]
        elif char == "万":
            total += (section + digit) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * UNITS[char]
            digit = 0
    return total + section + digit


def chapter_number(name: str) -> int | None:
    stem = PurePosixPath(unicodedata.normalize("NFKC", name)).stem
    match = re.search(r"第\s*([\d零〇一二三四五六七八九十百千万两]+)\s*[章集回节]", stem)
    if not match:
        match = re.match(r"\s*(\d+)(?:\D|$)", stem)
    return chinese_number(match.group(1)) if match else None


def volume_number(name: str) -> int:
    match = re.search(r"第\s*([\d零〇一二三四五六七八九十百千万两]+)\s*[部卷]", name)
    if match:
        return chinese_number(match.group(1))
    for marker, number in (("上部", 1), ("上卷", 1), ("中部", 2), ("中卷", 2), ("下部", 3), ("下卷", 3)):
        if marker in name:
            return number
    return 0


def safe_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    value = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", value).strip(" .")
    if not value or value in {".", ".."} or len(value.encode()) > 220:
        raise ValueError("名称不能为空，且不能超过 220 字节")
    return value


def analyze(names: list[str]) -> dict[str, object]:
    def key(entry: tuple[int, str]) -> tuple[int, int, int, int, str]:
        suffix = PurePosixPath(entry[1]).suffix.lower()
        kind = 0 if suffix in AUDIO else 1 if suffix in EBOOKS else 2
        number = chapter_number(entry[1]) if suffix in AUDIO else None
        return (kind, volume_number(entry[1]) if kind == 0 else 0, 0 if number is not None else 1, number or 0, entry[1].casefold())

    ordered = sorted(enumerate(names), key=key)
    volume_chapters = [(volume_number(name), chapter_number(name)) for name in names if PurePosixPath(name).suffix.lower() in AUDIO and chapter_number(name) is not None]
    duplicate_numbers = sorted({chapter for (_, chapter), count in Counter(volume_chapters).items() if count > 1})
    by_volume: dict[int, set[int]] = defaultdict(set)
    for volume, chapter in volume_chapters:
        by_volume[volume].add(chapter)
    missing_numbers: set[int] = set()
    for numbers in by_volume.values():
        if max(numbers) - min(numbers) < 20000:
            missing_numbers.update(set(range(min(numbers), max(numbers) + 1)) - numbers)
    missing = sorted(missing_numbers)[:100]
    folded = [unicodedata.normalize("NFC", name).casefold() for name in names]
    duplicate_names = sorted({names[i] for i, name in enumerate(folded) if folded.count(name) > 1})
    return {"order": [index for index, _ in ordered], "missing": missing, "duplicateNumbers": duplicate_numbers, "duplicateNames": duplicate_names}
