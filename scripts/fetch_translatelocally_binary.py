#!/usr/bin/env python3
from __future__ import annotations

import os
import platform
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path

import httpx

RELEASES_API = "https://api.github.com/repos/XapaJIaMnu/translateLocally/releases/latest"


def platform_tag() -> str:
    sysname = platform.system().lower()
    machine = platform.machine().lower()

    if machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        arch = "arm64"
    else:
        arch = machine

    if "linux" in sysname:
        return f"linux-{arch}"
    if "darwin" in sysname or "mac" in sysname:
        return f"macos-{arch}"
    if "windows" in sysname:
        return f"windows-{arch}"
    raise RuntimeError(f"Unsupported platform: {sysname}/{arch}")


def pick_asset(assets: list[dict], tag: str) -> dict | None:
    parts = set(tag.split("-"))

    def score(name: str) -> tuple[int, int]:
        low = name.lower()
        platform_hits = sum(1 for p in parts if p in low)
        ext_bonus = 0
        if low.endswith(".zip"):
            ext_bonus = 5
        elif low.endswith(".tar.gz") or low.endswith(".tgz"):
            ext_bonus = 4
        elif low.endswith(".appimage"):
            ext_bonus = 3
        elif low.endswith(".exe"):
            ext_bonus = 3
        elif low.endswith(".deb"):
            ext_bonus = 1
        return (platform_hits, ext_bonus)

    ranked = sorted(assets, key=lambda a: score(a.get("name", "")), reverse=True)
    for asset in ranked:
        name = asset.get("name", "")
        if score(name)[0] == 0:
            continue
        if name.lower().endswith(".deb"):
            continue
        return asset
    return None


def ensure_exec(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def extract_binary(download: Path, out_path: Path) -> None:
    low = download.name.lower()
    if low.endswith(".appimage") or low.endswith(".exe"):
        shutil.copy2(download, out_path)
        ensure_exec(out_path)
        return

    with tempfile.TemporaryDirectory(prefix="tl_extract_") as tmp:
        tmpdir = Path(tmp)
        if low.endswith(".zip"):
            with zipfile.ZipFile(download) as zf:
                zf.extractall(tmpdir)
        elif low.endswith(".tar.gz") or low.endswith(".tgz"):
            with tarfile.open(download, "r:gz") as tf:
                tf.extractall(tmpdir)
        else:
            raise RuntimeError(f"Unsupported archive format: {download.name}")

        for p in tmpdir.rglob("*"):
            if not p.is_file():
                continue
            n = p.name.lower()
            if n in {"translatelocally", "translatelocally.exe"}:
                shutil.copy2(p, out_path)
                ensure_exec(out_path)
                return

    raise RuntimeError("translateLocally executable not found inside downloaded asset")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tag = platform_tag()
    bin_dir = root / "src" / "tlptbr_translate" / "resources" / "bin" / tag
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe_name = "translateLocally.exe" if tag.startswith("windows") else "translateLocally"
    out_path = bin_dir / exe_name

    if out_path.exists():
        print(f"binary already present: {out_path}")
        return 0

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "fast-translate-ci",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(timeout=90.0, headers=headers) as client:
        rel = client.get(RELEASES_API)
        if rel.status_code == 403:
            msg = "GitHub API rate limit exceeded while fetching translateLocally release metadata."
            if not token:
                msg += " Set GITHUB_TOKEN/GH_TOKEN in CI to avoid anonymous rate limits."
            raise RuntimeError(msg)
        rel.raise_for_status()
        payload = rel.json()

    asset = pick_asset(payload.get("assets", []), tag)
    if not asset:
        raise RuntimeError(f"No release asset found for {tag}")

    url = asset["browser_download_url"]
    name = asset.get("name", Path(url).name)
    download_path = bin_dir / name

    with httpx.stream("GET", url, timeout=240.0) as resp:
        resp.raise_for_status()
        with download_path.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)

    extract_binary(download_path, out_path)
    print(f"saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
