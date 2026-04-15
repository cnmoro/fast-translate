from __future__ import annotations

import json
from pathlib import Path

from tlptbr_translate import runtime


def test_official_fallback_assets_macos_arm64() -> None:
    urls = runtime._official_fallback_assets("macos-arm64")
    assert any("macos-11.0.compat.dmg" in u for u in urls)
    assert not any("armv8.5-a" in u for u in urls)
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


def test_resolve_app_bundle_from_launcher_path(tmp_path: Path) -> None:
    app = tmp_path / "translateLocally.app"
    bin_path = app / "Contents" / "MacOS" / "translateLocally"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("", encoding="utf-8")
    assert runtime._resolve_app_bundle_from_binary(bin_path) == app


def test_prepare_runtime_models_prefers_translatelocally_container(tmp_path: Path, monkeypatch) -> None:
    src_models = tmp_path / "src_models"
    enpt = src_models / "enpt-1"
    enpt.mkdir(parents=True)
    (enpt / "model_info.json").write_text('{"shortName":"en-pt-tiny"}', encoding="utf-8")

    home = tmp_path / "home"
    (home / "Library" / "Containers" / "com.apple.Translate.TranslationAppIntentsExtension").mkdir(parents=True)
    (home / "Library" / "Containers" / "com.translatelocally.translateLocally").mkdir(parents=True)

    monkeypatch.setattr(runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(runtime.Path, "home", lambda: home)
    monkeypatch.setattr(runtime, "_darwin_container_model_roots", lambda binary_hint=None: [
        home / "Library" / "Containers" / "com.translatelocally.translateLocally" / "Data" / "Library" / "Application Support" / "translateLocally",
    ])

    chosen = runtime._prepare_runtime_models(src_models, binary_hint=None)
    assert "com.translatelocally.translateLocally" in str(chosen)
