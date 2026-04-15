from __future__ import annotations

import json
from pathlib import Path

from tlptbr_translate import runtime


def test_official_fallback_assets_macos_arm64() -> None:
    urls = runtime._official_fallback_assets("macos-arm64")
    assert any("macos-11.0.compat.dmg" in u for u in urls)
    assert all(u.startswith("https://translatelocally.com/files/latest/") for u in urls)


def test_asset_extension_allowed_filters_cross_platform() -> None:
    assert runtime._asset_extension_allowed("macos-arm64", "translateLocally.macos-11.0.compat.dmg")
    assert not runtime._asset_extension_allowed("macos-arm64", "translateLocally.windows-2022.core-avx-i.exe")
    assert runtime._asset_extension_allowed("linux-x86_64", "translateLocally.linux-22.04.x86-64.deb")
    assert not runtime._asset_extension_allowed("linux-x86_64", "translateLocally.macos-14.armv8.5-a.dmg")


def test_model_candidates_include_shortname_and_dir(tmp_path: Path) -> None:
    enpt = tmp_path / "enpt-123"
    enpt.mkdir()
    (enpt / "model_info.json").write_text(
        json.dumps({"shortName": "en-pt-tiny", "src": "English", "trg": "Portuguese"}),
        encoding="utf-8",
    )
    pten = tmp_path / "pten-456"
    pten.mkdir()
    (pten / "model_info.json").write_text(
        json.dumps({"shortName": "pt-en-tiny", "src": "Portuguese", "trg": "English"}),
        encoding="utf-8",
    )

    en_candidates = runtime._model_candidates_for_direction("en-pt", tmp_path)
    pt_candidates = runtime._model_candidates_for_direction("pt-en", tmp_path)

    assert "en-pt-tiny" in en_candidates
    assert "enpt-123" in en_candidates
    assert "pt-en-tiny" in pt_candidates
    assert "pten-456" in pt_candidates
