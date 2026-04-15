import json
from pathlib import Path

from fast_translate.runtime import get_models_root


def test_baked_models_exist() -> None:
    root = get_models_root()
    en_model = root / "enpt-1693087644" / "model_info.json"
    pt_model = root / "pten-1693087646" / "model_info.json"

    assert en_model.exists()
    assert pt_model.exists()

    en_info = json.loads(en_model.read_text(encoding="utf-8"))
    pt_info = json.loads(pt_model.read_text(encoding="utf-8"))
    assert en_info["shortName"] == "en-pt-tiny"
    assert pt_info["shortName"] == "pt-en-tiny"
