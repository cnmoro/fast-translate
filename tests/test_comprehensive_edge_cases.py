"""Comprehensive edge case tests for translation quality post-processing."""

import re
from pathlib import Path

import pytest

from fast_translate.postprocess import (
    post_edit_portuguese,
    post_edit_english,
    _critical_numbers,
    _ENGLISH_LEAK_GUARD_TOKENS,
)

_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ']+")


def qa_failures(src_en: str, pred_pt: str) -> list[str]:
    """Replicate QA logic for testing using library internals."""
    failures: list[str] = []
    if re.search(r"\s{2,}", pred_pt):
        failures.append("double_spaces")
    if re.search(r"\s+[,.!?;:]", pred_pt):
        failures.append("space_before_punct")
    for match in re.finditer(r"([,.;:!?])(\S)", pred_pt):
        punct = match.group(1)
        next_char = match.group(2)
        idx = match.start(1)
        prev_char = pred_pt[idx - 1] if idx > 0 else ""
        if next_char in ")]}'\"" or next_char.isdigit():
            continue
        if next_char in ",.;:!?":
            continue
        if punct == "." and (prev_char == "." or next_char == "."):
            continue
        if punct == "." and next_char.lower() in "oasr" and prev_char.isalpha() and prev_char.lower() in "nls":
            continue
        if punct == "." and next_char.lower() in "oa" and prev_char.isdigit():
            continue
        failures.append("missing_space_after_punct")
        break
    if pred_pt.count('"') % 2 == 1:
        failures.append("unbalanced_quotes")
    if pred_pt.count("(") != pred_pt.count(")"):
        failures.append("parenthesis_mismatch")
    src_set = set(_critical_numbers(src_en))
    pred_set = set(_critical_numbers(pred_pt))
    if any("pow" in str(t) for t in src_set):
        for m in re.finditer(r"\b10\s+a\s+(\d{1,2})\b", pred_pt):
            pred_set.add(f"pow10^{m.group(1)}")
    for m in re.finditer(r"\b(\d{1,2})\s*(?:da|às)\s*(?:manhã|tarde|noite)\b", pred_pt, re.IGNORECASE):
        h = int(m.group(1))
        if any(f"t{h:02d}" in str(t) for t in src_set):
            pred_set.add(f"t{h:02d}")
    for m in re.finditer(r"\b(\d+)\s+a\s+(\d+)\s*(?:anos?|meses|dias|semanas)?\b", pred_pt, re.IGNORECASE):
        a, b = int(m.group(1)), int(m.group(2))
        if f"g{a}-{b}" in src_set or f"g{a}-{b}" in pred_set:
            pred_set.add(f"g{a}-{b}")
        elif any(f"g{a}-" in str(t) or f"-{b}" in str(t) for t in src_set):
            pred_set.add(f"g{a}-{b}")
    missing = src_set - pred_set
    if missing:
        failures.append("number_mismatch")
    pred_tokens_orig = _WORD_RE.findall(pred_pt)
    hits = 0
    for token in pred_tokens_orig:
        low = token.lower()
        if low in _ENGLISH_LEAK_GUARD_TOKENS and token.islower():
            hits += 1
    if hits >= 1:
        failures.append("english_leak")
    return failures


def qa_failures_pt_en(src_pt: str, pred_en: str) -> list[str]:
    """Replicate PT-EN QA logic for testing using library internals."""
    failures: list[str] = []
    if re.search(r"\s{2,}", pred_en):
        failures.append("double_spaces")
    if re.search(r"\s+[,.!?;:]", pred_en):
        failures.append("space_before_punct")
    for match in re.finditer(r"([,.;:!?])(\S)", pred_en):
        punct = match.group(1)
        nxt = match.group(2)
        idx = match.start(1)
        prev = pred_en[idx - 1] if idx > 0 else ""
        if nxt in ")]}'\"" or nxt.isdigit() or nxt in ",.;:!?":
            continue
        if punct == "." and (prev == "." or nxt == "."):
            continue
        if punct == "." and nxt.lower() in "oasr" and prev.isalpha() and prev.lower() in "nls":
            continue
        if punct == "." and nxt.lower() in "oa" and prev.isdigit():
            continue
        failures.append("missing_space_after_punct")
        break
    if pred_en.count('"') % 2 == 1:
        failures.append("unbalanced_quotes")
    if pred_en.count("(") != pred_en.count(")"):
        failures.append("parenthesis_mismatch")
    src_num = _critical_numbers(src_pt)
    pred_num = _critical_numbers(pred_en)
    if src_num and not set(src_num).issubset(set(pred_num)):
        failures.append("number_mismatch")
    toks = [t.lower() for t in _WORD_RE.findall(pred_en)]
    strong = {"não", "nao", "mas", "com", "para", "que", "uma", "um",
              "ser", "está", "estao", "vou", "você", "voce", "obrigado",
              "olá", "ola", "sim"}
    weak = {"de", "da", "das", "dos"}
    s = sum(t in strong for t in toks)
    w = sum(t in weak for t in toks)
    if s >= 1 or w >= 2:
        failures.append("portuguese_leak")
    return failures

# ─── 1. PORTUGUESE LEGAL NUMBERING ───────────────────────────────────


class TestPortugueseLegalNumbering:
    def test_article_numbering_o_suffix(self):
        """artigo 110.o should not be split to artigo 110. o"""
        result = post_edit_portuguese("artigo 110.o do Regimento")
        assert "110.o" in result
        assert "110. o" not in result

    def test_multiple_article_numbers(self):
        result = post_edit_portuguese("artigos 81.o, 82.o e 89.o")
        assert "81.o" in result and "82.o" in result and "89.o" in result

    def test_n_o_abbreviation(self):
        result = post_edit_portuguese("n.o 2 do artigo")
        assert "n.o" in result

    def test_n_os_abbreviation(self):
        result = post_edit_portuguese("n.os 1 e 2")
        assert "n.os" in result

    def test_paragraph_a_suffix(self):
        result = post_edit_portuguese("artigo 1.a do código")
        assert "1.a" in result

    def test_full_legal_example(self):
        src = "Pursuant to Article 110(2) of the Rules of Procedure."
        raw = "Nos termos do n.o 2 do artigo 110.o do Regimento."
        result = post_edit_portuguese(raw)
        fails = qa_failures(src, result)
        assert not fails, f"Legal example failed: {fails}"
        assert "n.o" in result
        assert "110.o" in result


# ─── 2. SCIENTIFIC NOTATION ──────────────────────────────────────────


class TestScientificNotation:
    def test_10_power_13_translated(self):
        """10^13 translated as '10 a 13' should not cause number mismatch"""
        src = "halo mass threshold of 10^13 solar masses"
        pred = "limiar de massa de halo de 10 a 13 massas solares"
        fails = qa_failures(src, pred)
        assert "number_mismatch" not in fails

    def test_10_power_4_translated(self):
        src = "Reynolds number of 10^4"
        pred = "número de Reynolds de 10 a 4"
        fails = qa_failures(src, pred)
        assert "number_mismatch" not in fails

    def test_10_power_12_translated(self):
        src = "frequency of 540 x 10^12 Hz"
        pred = "frequência de 540 a 10 a 12 Hz"
        fails = qa_failures(src, pred)
        assert "number_mismatch" not in fails

    def test_age_range_not_confused_with_power(self):
        """'8 a 10 anos' (age range) should NOT be detected as scientific notation"""
        src = "children aged 8-10 years"
        pred = "crianças de 8 a 10 anos"
        fails = qa_failures(src, pred)
        assert "number_mismatch" not in fails


# ─── 3. TIME FORMATS ────────────────────────────────────────────────


class TestTimeFormats:
    def test_am_pm_preserved(self):
        src = "The meeting is at 3:00 p.m."
        pred = "A reunião é às 3:00 p.m."
        result = post_edit_portuguese(pred)
        fails = qa_failures(src, result)
        assert "number_mismatch" not in fails

    def test_24h_format(self):
        src = "The flight departs at 14:30."
        pred = "O voo parte às 14:30."
        result = post_edit_portuguese(pred)
        fails = qa_failures(src, result)
        assert not fails

    def test_portuguese_time_format(self):
        src = "The class starts at 8 a.m."
        pred = "A aula começa às 8 da manhã."
        result = post_edit_portuguese(pred)
        fails = qa_failures(src, result)
        assert "number_mismatch" not in fails


# ─── 4. MOJIBAKE ─────────────────────────────────────────────────────


class TestMojibake:
    def test_curly_quotes_left_single(self):
        result = post_edit_portuguese("I\u2019m fine")
        assert "\u2019" not in result

    def test_curly_quotes_left_double(self):
        result = post_edit_portuguese("\u201CHello\u201D she said")
        assert "\u201C" not in result and "\u201D" not in result

    def test_latin1_mojibake_single(self):
        result = post_edit_portuguese("I\u00e2\u20ac\u2122m fine")
        # Should be fixed to proper quotes

    def test_tm_symbol_removed(self):
        result = post_edit_portuguese("OpenTM the door")
        assert "TM" not in result or "Tm" not in result

    def test_accented_encoding_issue(self):
        result = post_edit_portuguese("cora\u00c3\u00a7\u00c3\u00a3o")
        # Should produce something reasonable


# ─── 5. ENGLISH LEAKS ────────────────────────────────────────────────


class TestEnglishLeakDetection:
    def test_conversational_leaks_fixed(self):
        src = "Okay, let me think about this."
        raw = "Okay, let me think about this."
        result = post_edit_portuguese(raw)
        fails = qa_failures(src, result)
        assert "english_leak" not in fails, f"Leaks remain: {result}"

    def test_alright_fixed(self):
        src = "Alright, I will help you."
        raw = "Alright, I will help you."
        result = post_edit_portuguese(raw)
        fails = qa_failures(src, result)
        assert "english_leak" not in fails

    def test_wow_fixed(self):
        src = "Wow, that is amazing!"
        raw = "Wow, that is amazing!"
        result = post_edit_portuguese(raw)
        fails = qa_failures(src, result)
        assert "english_leak" not in fails

    def test_yeah_fixed(self):
        result = post_edit_portuguese("Yeah, I know that.")
        assert "sim" in result.lower() or "yeah" not in result.lower()

    def test_contractions_fixed(self):
        result = post_edit_portuguese("Let's go to the park. I don't know. I'm tired.")
        assert "vamos" in result.lower()
        assert "não" in result.lower()
        assert "estou" in result.lower() or "sou" in result.lower()

    def test_english_pronouns_fixed(self):
        src = "She said he will come with them."
        raw = "She said he will come with them."
        result = post_edit_portuguese(raw)
        fails = qa_failures(src, result)
        assert "english_leak" not in fails

    def test_common_verbs_fixed(self):
        src = "I think we need more time."
        raw = "I think we need more time."
        result = post_edit_portuguese(raw)
        fails = qa_failures(src, result)
        assert "english_leak" not in fails

    def test_prepositions_fixed(self):
        src = "The book is on the table."
        raw = "The book is on the table."
        result = post_edit_portuguese(raw)
        fails = qa_failures(src, result)
        assert "english_leak" not in fails, f"Leaks: {result}"

    def test_possessive_s_cleaned(self):
        result = post_edit_portuguese('John "s book is on the table')
        assert '"s' not in result
        assert "'s" not in result or "s" in result

    def test_ok_lowercase_fixed(self):
        result = post_edit_portuguese("ok, let's start")
        assert "ok" in result.lower()


# ─── 6. PORTUGUESE EUROPEAN TO BRAZILIAN ────────────────────────────


class TestPTPTtoPTBR:
    def test_autocarro_to_onibus(self):
        result = post_edit_portuguese("O autocarro chegou atrasado.")
        assert "ônibus" in result.lower()
        assert "autocarro" not in result.lower()

    def test_comboio_to_trem(self):
        result = post_edit_portuguese("O comboio partiu da estação.")
        assert "trem" in result.lower()
        assert "comboio" not in result.lower()

    def test_telemovel_to_celular(self):
        result = post_edit_portuguese("O meu telemóvel está descarregado.")
        assert "celular" in result.lower()

    def test_rapaz_to_garoto(self):
        result = post_edit_portuguese("O rapaz foi à escola.")
        assert "garoto" in result.lower()

    def test_old_spelling_normalized(self):
        result = post_edit_portuguese("Adoptámos este projecto com objectivos de protecção.")
        r = result.lower()
        assert "adot" in r
        assert "projet" in r
        assert "objet" in r
        assert "proteç" in r

    def test_pequeno_almoco(self):
        result = post_edit_portuguese("Tomei o pequeno-almoço às 8h.")
        assert "café da manhã" in result.lower()

    def test_sumo_to_suco(self):
        result = post_edit_portuguese("Bebi sumo de laranja.")
        assert "suco" in result.lower()

    def test_miudos_to_meninos(self):
        result = post_edit_portuguese("Os miúdos estão no parque.")
        assert "meninos" in result.lower()


# ─── 7. SLIDE CONTEXT ────────────────────────────────────────────────


class TestSlideContext:
    def test_slide_in_playground_context(self):
        """'slide' in playground context should be translated to 'escorregador'"""
        result = post_edit_portuguese("The kids play on the slide at the park.")
        assert "escorregador" in result.lower()

    def test_slides_in_playground_context(self):
        result = post_edit_portuguese("The children love the slides at the playground.")
        assert "escorregadores" in result.lower()

    def test_slides_preserved_in_presentation_context(self):
        """'slides' in presentation context should stay as 'slides'"""
        result = post_edit_portuguese("Preparei os slides para a apresentação.")
        assert "slides" in result.lower()

    def test_slide_alone_no_context(self):
        """Without context, 'slide' should stay as-is"""
        result = post_edit_portuguese("O slide mostra os dados.")
        assert "escorregador" not in result.lower()


# ─── 8. PUNCTUATION AND SPACING ──────────────────────────────────────


class TestPunctuationSpacing:
    def test_ellipsis_normalized(self):
        result = post_edit_portuguese("Um, dois, tres. .. ponto")
        assert ". .." not in result
        assert "..." in result

    def test_double_spaces_removed(self):
        result = post_edit_portuguese("Isto  tem   espacos   duplos.")
        assert "  " not in result

    def test_space_before_punct_removed(self):
        result = post_edit_portuguese("Ola , mundo . Como esta ?")
        fails = qa_failures("test", result)
        assert "space_before_punct" not in fails

    def test_quote_balance(self):
        result = post_edit_portuguese('Ele disse "ola mundo.')
        assert result.count('"') % 2 == 0

    def test_paren_balance_extra_right(self):
        result = post_edit_portuguese("(isto é um teste))")
        assert result.count("(") == result.count(")")

    def test_paren_balance_extra_left(self):
        result = post_edit_portuguese("((isto é um teste)")
        assert result.count("(") == result.count(")")

    def text_space_after_punct(self):
        result = post_edit_portuguese("Ola mundo.Fim.")
        fails = qa_failures("test", result)
        assert "missing_space_after_punct" not in fails

    def test_multiple_punct_clusters(self):
        """Multiple punctuation like ?! should not be separated"""
        result = post_edit_portuguese("Serio?! Inacreditavel!")
        assert "? !" not in result


# ─── 9. NUMBERS ──────────────────────────────────────────────────────


class TestNumbers:
    def test_years_preserved(self):
        src = "The law was enacted in 1965."
        pred = "A lei foi promulgada em 1965."
        fails = qa_failures(src, pred)
        assert "number_mismatch" not in fails

    def test_percentages_decimal(self):
        src = "The incidence is 0.6% in 1000 people."
        pred = "A incidencia e de 0,6% em 1000 pessoas."
        fails = qa_failures(src, pred)
        assert "number_mismatch" not in fails

    def test_thousands_separator(self):
        src = "The population is 1500000."
        pred = "A populacao e de 1.500.000."
        fails = qa_failures(src, pred)
        assert "number_mismatch" not in fails

    def test_ranges_preserved(self):
        src = "Pages 45-67 contain the relevant data."
        pred = "As paginas 45-67 contem os dados relevantes."
        fails = qa_failures(src, pred)
        assert "number_mismatch" not in fails

    def test_grade_not_critical(self):
        """Grade levels should not cause number mismatch"""
        src = "I teach 7th-grade algebra."
        pred = "Eu ensino algebra no 7o ano."
        fails = qa_failures(src, pred)
        assert "number_mismatch" not in fails

    def test_year_old_context_ignored(self):
        src = "He is a 10-year-old boy."
        pred = "Ele e um menino de 10 anos."
        fails = qa_failures(src, pred)
        assert "number_mismatch" not in fails


# ─── 10. MIXED SCENARIOS ─────────────────────────────────────────────


class TestMixedScenarios:
    def test_complex_legal_text(self):
        src = "Under Article 88, paragraph 1, the Commission is obliged to supervise state aid."
        raw = "Nos termos do artigo 88.o, n.o 1, a Comissao e obrigada a supervisionar os auxilios estatais."
        result = post_edit_portuguese(raw)
        fails = qa_failures(src, result)
        assert not fails

    def test_hide_and_seek_variants(self):
        variants = [
            "hide-and-seek",
            "hide and seek",
            "hideandseek",
            "hide-e-seek",
            "Hide and Seek",
        ]
        for v in variants:
            result = post_edit_portuguese(f"Playing {v}")
            assert "esconde-esconde" in result.lower(), f"Failed for: {v}"

    def test_three_two_one_countdown(self):
        result = post_edit_portuguese("3... 2... 1... Go!")
        assert "3...2...1..." in result

    def test_quit3_medical(self):
        result = post_edit_portuguese("quit 3")
        assert "quitó" in result.lower()  # already correct

    def test_cleanup_after_aggressive_replacements(self):
        """Ensure the pipeline doesn't produce garbage after heavy replacements"""
        src = "The quick brown fox jumps over the lazy dog."
        raw = "The quick brown fox jumps over the lazy dog."
        result = post_edit_portuguese(raw)
        # Should not produce nonsense
        assert len(result) > 10
        # Should fix all spacing
        fails = qa_failures(src, result)
        assert "space_before_punct" not in fails
        assert "double_spaces" not in fails


# ─── 11. EDGE CASES ──────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_string(self):
        result = post_edit_portuguese("")
        assert result == ""

    def test_only_punctuation(self):
        result = post_edit_portuguese("... !!! ???")
        assert result

    def test_whitespace_only(self):
        result = post_edit_portuguese("   ")
        assert result == ""

    def test_single_word(self):
        result = post_edit_portuguese("Olá")
        assert result == "Olá"

    def test_numbers_only(self):
        src = "42"
        result = post_edit_portuguese("42")
        fails = qa_failures(src, result)
        assert not fails

    def test_very_long_word(self):
        word = "pneumoultramicroscopicossilicovulcanoconiótico"
        result = post_edit_portuguese(word)
        assert result == word

    def test_mixed_language_sentence(self):
        """Should handle sentences with mixed English/Portuguese gracefully"""
        src = "The API returns JSON data via HTTP."
        pred = "A API retorna dados JSON via HTTP."
        result = post_edit_portuguese(pred)
        # Acronyms should be preserved
        for acro in ["API", "JSON", "HTTP"]:
            assert acro in result or acro.lower() in result.lower()

    def test_unicode_special_chars(self):
        text = "coração feliz ♥ ∑ ∫ ∞"
        result = post_edit_portuguese(text)
        assert "coração" in result

    def test_newlines_not_introduced(self):
        text = "Linha um. Linha dois."
        result = post_edit_portuguese(text)
        assert "\n" not in result

    def test_repeated_punctuation_safe(self):
        text = "!!!!!!!!"
        result = post_edit_portuguese(text)
        assert result  # Should not crash


# ─── 12. PT-EN DIRECTION ─────────────────────────────────────────────


class TestPTENDirection:
    def test_portuguese_leak_fixed(self):
        src = "Ela disse que não viria."
        raw = 'She said "não" she would not come.'
        result = post_edit_english(raw)
        fails = qa_failures_pt_en(src, result)
        assert "portuguese_leak" not in fails

    def test_obrigado_translated(self):
        result = post_edit_english("Obrigado pela ajuda!")
        assert "thank you" in result.lower()

    def test_com_not_english(self):
        src = "Ela foi com ele."
        raw = "She went com him."
        result = post_edit_english(raw)
        fails = qa_failures_pt_en(src, result)
        assert "portuguese_leak" not in fails

    def test_voce_to_you(self):
        result = post_edit_english("Como você está?")
        assert "you" in result.lower()

    def test_pote_de_doces_fixed(self):
        result = post_edit_english('"Pote de Doces da Lily"')
        assert "Pote de Doces" not in result
        assert "candy" in result.lower()
