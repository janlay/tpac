"""Access point firmware packages: update check, download, and image check."""
from __future__ import annotations

import base64
import hashlib
import re
import struct
import time
import zipfile
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from aiohttp import ClientSession, ClientTimeout

SEARCH_URL = "https://resource.tp-link.com.cn/api/v1/material-center/search"
SEARCH_PAGE_SIZE = 24
SEARCH_PAGES = 3
REQUEST_HEADERS = {
    "Referer": "https://resource.tp-link.com.cn/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5.2 Safari/605.1.15",
}
PACKAGE_RETENTION = 30 * 24 * 3600
TITLE = re.compile(r"^(?P<model>.+?) V(?P<hardware>\d+\.\d+)升级软件(?P<date>\d{8})_(?P<version>\d+\.\d+\.\d+)$")
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
SIGNATURE_OFFSET = 0x20
SIGNATURE_LENGTH = 0x80
KEY_INDEX_OFFSET = 0x1a
MIN_IMAGE_OFFSET = 0x18
PREFIX_SHA1 = bytes.fromhex("3021300906052b0e03021a05000414")

AP_KEYS = {
    0x00: "BgIAAACkAABSU0ExAAQAAAEAAQA1Ccyu85b65TawjvSQTaryGNk1gBJVn6kEIJq6m0hagsqkiy32v4ui41ucp6tKfaoqb7AHDBq41dcEMgM6YBF2e3aRKQqZ6EwgCvAi3O81n7UbE97lD+FhvqlYxyqqMbSdvNmCiAoujheUs9DUaOCHq4K3McDxATMVOnCtT1H+wQ==",
    0x01: "BgIAAACkAABSU0ExAAQAAAEAAQD7Bk7f7fdnL9drucbr+P9wA2JUlYP/OH4zvIS69eY3KKmUB1fs9ND06EINqTQ4vQ4gCeekU1dRi3WiZLgVjo/UzovplddUezNMWq0gk4TVbsGf/xzXZN+pDWid9zYsSr9qvINId6cnMR+s/wXB1TOE6t6wfzHvnbkJR0r1mqG4yA==",
    0x02: "BgIAAACkAABSU0ExAAQAAAEAAQAHNt5fFl0BUlLkPjKJloZFlVFkegFjEsVJCRjwbRD6i646tpvc/Z5MK6SuXcz3yizxDGMnZ6BJdqCR9SJTdd3b11F7Q+pgetcAgX5X9NZTzo1MCvpkKAlEyZG0rXMpSbADNNqtACNT0BLhHu4nyiDBBIIOSZljQAzHiqSquxHDsg==",
    0x35: "BgIAAACkAABSU0ExAAQAAAEAAQArjNXuvBeCGfOD19AGJGmceW+ip5W76C+sOHk0bJgrtZhk+t/ZzZwAv/TLA+MwNipNZSd+fOysmqDsA53cEIKdzor0WbWGq0n/BYr1o8fh4Pm656mOn9C6LH6nCf6w48Nog84Pc+NuwHcB93p6Wj0y3YVl8sGn+eeokA8ltZzLnA==",
}


@dataclass(frozen=True)
class Package:

    model: str
    hardware: str
    version: tuple[int, ...]
    title: str
    url: str


def parse_version(text: str) -> tuple[int, ...] | None:
    match = VERSION.match(text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def parse_title(title: str, url: str) -> Package | None:
    match = TITLE.match(title)
    if match is None:
        return None
    version = parse_version(match["version"])
    return Package(match["model"], match["hardware"], version, title, url)


def search_keyword(model: str) -> str:
    return model.split("-PoE")[0]


async def find_latest(session: ClientSession, model: str, hardware: str) -> Package | None:
    latest = None
    for page in range(1, SEARCH_PAGES + 1):
        body = {
            "crossFilter": [], "formats": ["zip", "rar"], "productClassIds": None,
            "pageIndex": page, "pageSize": SEARCH_PAGE_SIZE, "sortOrder": "DEFAULT",
            "orderDirection": "DESC", "isMaterial": True, "keyword": search_keyword(model), "tagIds": None,
        }
        async with session.post(SEARCH_URL, json=body, headers=REQUEST_HEADERS) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        if data.get("errorCode") != 0:
            raise RuntimeError(f"search failed: {data.get('message')}")
        items = data["result"]["collection"]
        for item in items:
            if item.get("softwareType") != "UPGRADE_SOFT":
                continue
            package = parse_title(item["title"], item["url"])
            if package is None or package.model != model or package.hardware != hardware:
                continue
            if latest is None or package.version > latest.version:
                latest = package
        if len(items) < SEARCH_PAGE_SIZE:
            break
    return latest


async def download_package(session: ClientSession, url: str) -> bytes:
    timeout = ClientTimeout(total=600)
    async with session.get(quote(url, safe=":/"), headers=REQUEST_HEADERS, timeout=timeout) as response:
        response.raise_for_status()
        return await response.read()


def package_path(directory, url: str):
    return directory / unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def store_package(directory, url: str, data: bytes):
    directory.mkdir(parents=True, exist_ok=True)
    target = package_path(directory, url)
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    part.replace(target)
    return target


def remove_old_packages(directory, now: float | None = None) -> list[str]:
    if not directory.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - PACKAGE_RETENTION
    removed = []
    for path in directory.glob("*.zip"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


def read_image(package) -> bytes:
    with zipfile.ZipFile(package) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".bin")]
        if len(names) != 1:
            raise ValueError(f"expected one .bin in the package, found {len(names)}")
        return archive.read(names[0])


IMAGE_NAME = re.compile(r"_(?P<version>\d+\.\d+\.\d+)_Build_(?P<date>\d{8})_Rel\.(?P<release>\d+)\.bin$", re.IGNORECASE)


def image_version_record(package) -> bytes:
    from .upgrade import version_record

    with zipfile.ZipFile(package) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".bin")]
    if len(names) != 1:
        raise ValueError("expected one .bin in the package")
    match = IMAGE_NAME.search(names[0].rsplit("/", 1)[-1])
    if match is None:
        raise ValueError("image name does not carry version, build date and release")
    patch = int(match["version"].split(".")[2])
    return version_record(patch, match["date"], int(match["release"]))


def _public_key(key_b64: str) -> tuple[int, int, int]:
    blob = base64.b64decode(key_b64)
    if blob[8:12] != b"RSA1":
        raise ValueError("not an RSA public key blob")
    bits = struct.unpack_from("<I", blob, 12)[0]
    exponent = struct.unpack_from("<I", blob, 16)[0]
    modulus = int.from_bytes(blob[20:20 + bits // 8][::-1], "big")
    return modulus, exponent, bits // 8


def verify_image(image: bytes) -> str:
    if len(image) < struct.unpack_from(">H", image, MIN_IMAGE_OFFSET)[0]:
        return "image is smaller than its header states"
    index = struct.unpack_from(">H", image, KEY_INDEX_OFFSET)[0]
    if index not in AP_KEYS:
        return f"unknown key index {index:#x}"
    modulus, exponent, length = _public_key(AP_KEYS[index])
    end = SIGNATURE_OFFSET + SIGNATURE_LENGTH
    signed = bytearray(image)
    signed[SIGNATURE_OFFSET:end] = bytes(SIGNATURE_LENGTH)
    digest = hashlib.md5(signed).digest()
    value = int.from_bytes(image[SIGNATURE_OFFSET:end][::-1], "big")
    if value >= modulus:
        return "signature is out of range"
    block = pow(value, exponent, modulus).to_bytes(length, "big")
    if block[:2] != b"\x00\x01":
        return "bad block type"
    pad = 2
    while pad < len(block) and block[pad] == 0xFF:
        pad += 1
    if pad >= len(block) or block[pad] != 0 or pad < 10:
        return "bad padding"
    if block[pad + 1:] != PREFIX_SHA1 + hashlib.sha1(digest).digest():
        return "digest mismatch"
    return ""
