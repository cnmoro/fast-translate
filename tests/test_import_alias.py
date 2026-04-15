from fast_translate import TranslationError, Translator


def test_fast_translate_import_alias() -> None:
    assert Translator is not None
    assert TranslationError is not None
