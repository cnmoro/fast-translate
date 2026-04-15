#!/usr/bin/env python3
from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
import io
from pathlib import Path

import httpx
import zstandard as zstd

REPO_API_BASE = "https://api.github.com/repos/XapaJIaMnu/translateLocally"
RELEASES_LATEST_API = f"{REPO_API_BASE}/releases/latest"
RELEASES_LIST_API = f"{REPO_API_BASE}/releases"
OFFICIAL_FILES_BASE = "https://translatelocally.com/files/latest"


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
    parts = asset_match_markers(tag)
    host_macos_major = host_macos_major_version()

    def score(name: str) -> tuple[int, int]:
        low = name.lower()
        platform_hits = sum(1 for p in parts if p in low)
        ext_bonus = 0
        penalty = 0
        mac_bonus = 0
        if tag.startswith("linux") and low.endswith(".deb"):
            ext_bonus = 6
        if "core-avx" in low or ".avx" in low or "avx." in low:
            penalty = -2
        if tag.startswith("macos"):
            asset_major = asset_macos_major(low)
            if host_macos_major is not None and asset_major is not None:
                if asset_major > host_macos_major:
                    penalty -= 20
                else:
                    mac_bonus += max(0, 8 - (host_macos_major - asset_major))
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
        return (platform_hits + penalty + mac_bonus, ext_bonus)

    ranked = sorted(assets, key=lambda a: score(a.get("name", "")), reverse=True)
    for asset in ranked:
        name = asset.get("name", "")
        low = name.lower()
        if not asset_extension_allowed(tag, low):
            continue
        if score(name)[0] == 0 and not (tag.startswith("linux") and low.endswith(".deb")):
            continue
        return asset
    return None


def asset_match_markers(tag: str) -> set[str]:
    markers = set(tag.split("-"))
    low_tag = tag.lower()

    if "linux" in low_tag:
        markers.update({"linux", "ubuntu", "debian"})
    if "macos" in low_tag or "darwin" in low_tag:
        markers.update({"macos", "darwin", "osx"})
    if "windows" in low_tag:
        markers.update({"windows", "win"})

    if "x86_64" in low_tag or "amd64" in low_tag:
        markers.update({"x86_64", "x86-64", "amd64", "x64"})
    if "arm64" in low_tag or "aarch64" in low_tag:
        markers.update({"arm64", "aarch64", "armv8", "armv8.5-a"})

    return markers


def asset_extension_allowed(tag: str, name_lower: str) -> bool:
    if tag.startswith("macos"):
        return name_lower.endswith(".dmg")
    if tag.startswith("windows"):
        return name_lower.endswith(".exe") or name_lower.endswith(".zip")
    if tag.startswith("linux"):
        return (
            name_lower.endswith(".deb")
            or name_lower.endswith(".appimage")
            or name_lower.endswith(".tar.gz")
            or name_lower.endswith(".tgz")
            or name_lower.endswith(".zip")
        )
    return True


def host_macos_major_version() -> int | None:
    if platform.system().lower() != "darwin":
        return None
    ver = platform.mac_ver()[0]
    if not ver:
        return None
    try:
        return int(ver.split(".")[0])
    except Exception:
        return None


def asset_macos_major(name: str) -> int | None:
    m = re.search(r"macos-(\d+)", name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
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
    if low.endswith(".deb"):
        _extract_from_deb(download, out_path)
        return
    if low.endswith(".dmg"):
        _extract_from_dmg(download, out_path)
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


def _extract_from_deb(deb_path: Path, out_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="tl_deb_") as tmp:
        tmpdir = Path(tmp)
        root = tmpdir / "root"
        root.mkdir(parents=True, exist_ok=True)

        data_name, data_bytes = _read_data_tar_from_deb(deb_path)
        tar_stream = _decompress_data_member(data_name, data_bytes)
        with tarfile.open(fileobj=io.BytesIO(tar_stream), mode="r:") as tf:
            tf.extractall(root)

        candidate = root / "usr" / "bin" / "translateLocally"
        if not candidate.exists():
            hits = list(root.rglob("translateLocally"))
            if not hits:
                raise RuntimeError("translateLocally binary not found inside .deb")
            candidate = hits[0]
        shutil.copy2(candidate, out_path)
        ensure_exec(out_path)


def _read_data_tar_from_deb(deb_path: Path) -> tuple[str, bytes]:
    raw = deb_path.read_bytes()
    if not raw.startswith(b"!<arch>\n"):
        raise RuntimeError(f"Invalid .deb archive format: {deb_path}")
    pos = 8
    while pos + 60 <= len(raw):
        hdr = raw[pos : pos + 60]
        name = hdr[0:16].decode("utf-8", errors="replace").strip().rstrip("/")
        size_str = hdr[48:58].decode("utf-8", errors="replace").strip()
        size = int(size_str)
        pos += 60
        data = raw[pos : pos + size]
        pos += size + (size % 2)
        if name.startswith("data.tar"):
            return name, data
    raise RuntimeError(f"Could not find data.tar.* inside .deb: {deb_path}")


def _decompress_data_member(name: str, data: bytes) -> bytes:
    low = name.lower()
    if low.endswith(".zst"):
        return zstd.ZstdDecompressor().decompress(data)
    if low.endswith(".xz"):
        import lzma

        return lzma.decompress(data)
    if low.endswith(".gz"):
        import gzip

        return gzip.decompress(data)
    if low.endswith(".bz2"):
        import bz2

        return bz2.decompress(data)
    return data


def _extract_from_dmg(dmg_path: Path, out_path: Path) -> None:
    if platform.system().lower() != "darwin":
        raise RuntimeError("DMG extraction is only supported on macOS runners")

    attach = subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-readonly", str(dmg_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    mount_points: list[str] = []
    for line in attach.stdout.splitlines():
        cols = line.split("\t")
        if cols and cols[-1].startswith("/Volumes/"):
            mount_points.append(cols[-1].strip())

    if not mount_points:
        raise RuntimeError("Could not determine DMG mount point")
    mount = Path(mount_points[-1])

    try:
        app_candidates = list(mount.rglob("translateLocally.app"))
        if app_candidates:
            app_src = app_candidates[0]
            app_dst = out_path.parent / "translateLocally.app"
            if app_dst.exists():
                shutil.rmtree(app_dst)
            shutil.copytree(app_src, app_dst, symlinks=True)

            if platform.system().lower() == "darwin":
                launcher = (
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    "DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
                    "exec \"$DIR/translateLocally.app/Contents/MacOS/translateLocally\" \"$@\"\n"
                )
                out_path.write_text(launcher, encoding="utf-8")
                ensure_exec(out_path)
                return

        candidates = list(mount.rglob("translateLocally"))
        if not candidates:
            raise RuntimeError("translateLocally binary not found inside DMG")
        shutil.copy2(candidates[0], out_path)
        ensure_exec(out_path)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount)], check=False, capture_output=True)


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
        payload = fetch_release_payload(client=client, has_token=bool(token))

    candidate_urls: list[str] = []
    asset = pick_asset(payload.get("assets", []), tag)
    if asset:
        candidate_urls.append(asset["browser_download_url"])
    for url in official_fallback_assets(tag):
        if url not in candidate_urls:
            candidate_urls.append(url)
    if not candidate_urls:
        raise RuntimeError(f"No release asset found for {tag}")

    last_error = ""
    for url in candidate_urls:
        name = Path(url).name
        download_path = bin_dir / name
        try:
            with httpx.stream("GET", url, timeout=240.0, follow_redirects=True) as resp:
                resp.raise_for_status()
                with download_path.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
            extract_binary(download_path, out_path)
            print(f"saved: {out_path}")
            return 0
        except Exception as exc:
            last_error = f"{url}: {exc}"
            continue

    raise RuntimeError(f"All binary download candidates failed for {tag}. Last error: {last_error}")


def official_fallback_assets(tag: str) -> list[str]:
    if tag == "macos-arm64":
        return [
            f"{OFFICIAL_FILES_BASE}/translateLocally.macos-11.0.compat.dmg",
            f"{OFFICIAL_FILES_BASE}/translateLocally.macos-14.armv8.5-a.dmg",
        ]
    if tag == "macos-x86_64":
        return [
            f"{OFFICIAL_FILES_BASE}/translateLocally.macos-11.0.compat.dmg",
            f"{OFFICIAL_FILES_BASE}/translateLocally.macos-13.x86-64.dmg",
            f"{OFFICIAL_FILES_BASE}/translateLocally.macos-12.x86-64.dmg",
        ]
    return []


def fetch_release_payload(client: httpx.Client, has_token: bool) -> dict:
    rel = client.get(RELEASES_LATEST_API)
    if rel.status_code == 403:
        msg = "GitHub API rate limit exceeded while fetching translateLocally release metadata."
        if not has_token:
            msg += " Set GITHUB_TOKEN/GH_TOKEN in CI to avoid anonymous rate limits."
        raise RuntimeError(msg)
    if rel.status_code == 404:
        # Some repositories don't expose /releases/latest (only drafts/prereleases).
        lst = client.get(RELEASES_LIST_API)
        if lst.status_code == 403:
            msg = "GitHub API rate limit exceeded while fetching translateLocally release list."
            if not has_token:
                msg += " Set GITHUB_TOKEN/GH_TOKEN in CI to avoid anonymous rate limits."
            raise RuntimeError(msg)
        lst.raise_for_status()
        releases = lst.json()
        if not isinstance(releases, list) or not releases:
            raise RuntimeError("No releases found for translateLocally repository.")
        for release in releases:
            if release.get("draft"):
                continue
            if release.get("assets"):
                return release
        return releases[0]

    rel.raise_for_status()
    return rel.json()


if __name__ == "__main__":
    raise SystemExit(main())
