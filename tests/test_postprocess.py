from pathlib import Path

from tlptbr_translate.postprocess import postprocess


def test_slide_context_playground() -> None:
    out = postprocess("The kids are on the slide in the playground.", direction="en-pt").lower()
    assert "escorregador" in out


def test_slide_context_presentation() -> None:
    out = postprocess("Preparei os slides para a apresentação.", direction="en-pt").lower()
    assert "slides" in out


def test_ptpt_to_ptbr_normalization() -> None:
    out = postprocess("Ao adoptarmos este projecto, o objectivo é ampliar a protecção.", direction="en-pt").lower()
    assert "adopt" not in out
    assert "project" not in out
    assert "object" not in out
    assert "protecç" not in out
    assert "adot" in out
    assert "projet" in out
    assert "objet" in out
    assert "proteç" in out
