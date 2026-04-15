from __future__ import annotations

import re

_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ']+")

_TOKEN_REPLACEMENTS = {
    "tag": "pega-pega",
    "mixer": "misturador",
    "mom": "mamãe",
    "dad": "papai",
    "sock": "meia",
    "frog": "sapo",
    "the": "o",
    "and": "e",
    "this": "isto",
    "that": "isso",
    "with": "com",
    "your": "seu",
    "was": "era",
    "where": "onde",
    "what": "o que",
    "not": "não",
    "but": "mas",
    "are": "são",
    "have": "ter",
    "from": "de",
    "when": "quando",
    "could": "poderia",
    "into": "em",
    "hide": "esconde",
    "seek": "procura",
    "sure": "claro",
    "yes": "sim",
    "no": "não",
    "go": "ir",
    "job": "trabalho",
    "twinkle": "brilha",
    "said": "disse",
    "look": "olhe",
}

_SLIDE_PLAYGROUND_CONTEXT = {
    "playground", "park", "kid", "kids", "child", "children", "swing",
    "sandbox", "ladder", "climb", "climbed", "parquinho", "parque", "balanço",
}

_SLIDE_PRESENTATION_CONTEXT = {
    "presentation", "presentations", "powerpoint", "deck", "meeting", "workshop",
    "class", "lecture", "apresentação", "apresentacoes", "palestra", "reunião", "aula",
}

_PTPT_TO_PTBR_PHRASE_REPLACEMENTS: list[tuple[str, str]] = [
    (r"\bpequeno-almoço\b", "café da manhã"),
]

_PTPT_TO_PTBR_TOKEN_REPLACEMENTS: list[tuple[str, str]] = [
    (r"\bautocarros\b", "ônibus"),
    (r"\bautocarro\b", "ônibus"),
    (r"\bcomboios\b", "trens"),
    (r"\bcomboio\b", "trem"),
    (r"\btelemóveis\b", "celulares"),
    (r"\btelemóvel\b", "celular"),
    (r"\bpeúgas\b", "meias"),
    (r"\bpeúga\b", "meia"),
    (r"\bmiúdos\b", "meninos"),
    (r"\bmiúdo\b", "menino"),
    (r"\bmiúdas\b", "meninas"),
    (r"\bmiúda\b", "menina"),
    (r"\braparigas\b", "garotas"),
    (r"\brapariga\b", "garota"),
    (r"\brapazes\b", "garotos"),
    (r"\brapaz\b", "garoto"),
    (r"\bsumo\b", "suco"),
    (r"\bfixe\b", "legal"),
    (r"\besplanada\b", "área externa"),
    (r"\brebuçados\b", "balas"),
    (r"\brebuçado\b", "bala"),
    (r"\bduche\b", "banho"),
    (r"\bpropina\b", "mensalidade"),
    (r"\bencarnad([oa]s?)\b", r"vermelh\1"),
    (r"\badopt", "adot"),
    (r"\bobject", "objet"),
    (r"\bproject", "projet"),
    (r"\bprotecç", "proteç"),
    (r"\bdirecç", "direç"),
    (r"\bselecç", "seleç"),
    (r"\bcolecç", "coleç"),
    (r"\bacç", "aç"),
    (r"\bafect", "afet"),
    (r"\befect", "efet"),
    (r"\bópt", "ót"),
]

_PORTUGUESE_LEAK_REPLACEMENTS = {
    r"\b[Nn]ão\b": "not",
    r"\b[Nn]ao\b": "not",
    r"\b[Mm]as\b": "but",
    r"\b[Cc]om\b": "with",
    r"\b[Pp]ara\b": "for",
    r"\b[Oo]brigado\b": "thank you",
    r"\b[Ss]im\b": "yes",
    r"\b[Oo]lá\b": "hello",
    r"\b[Oo]la\b": "hello",
    r"\b[Vv]ocê\b": "you",
    r"\b[Vv]oce\b": "you",
    r"\b[Bb]arriguinha\b": "belly",
}


def post_edit_portuguese(text: str) -> str:
    text = _fix_common_spacing_and_encoding(text)

    text = re.sub(
        r"\bhide\s*-\s*and\s*-\s*seek\b|\bhide\s+and\s+seek\b|\bhideandseek\b",
        "esconde-esconde",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bhide\s*-\s*e\s*-\s*seek\b|\besconde\s*-\s*e\s*-\s*seek\b", "esconde-esconde", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[Ll]et['’]?s\b", "vamos", text)
    text = re.sub(r"\b[Dd]on['’]?t\b", "não", text)
    text = re.sub(r"\b[Ii]['’]?m\b", "eu estou", text)
    text = re.sub(r"\b[Ii]\b", "eu", text)
    text = re.sub(r"\b[Ss]aid\b", "disse", text)
    text = re.sub(r"\b[Ll]ook\b", "olhe", text)
    text = _replace_slide_contextual(text)
    text = _normalize_ptpt_to_ptbr(text)
    text = re.sub(r"\b3\s*\.\.\.\s*2\s*\.\.\.(?!\s*1)", "3...2...1...", text)

    for src, tgt in _TOKEN_REPLACEMENTS.items():
        text = re.sub(rf"\b{re.escape(src)}\b", tgt, text)

    text = re.sub(r"\b([A-Za-zÀ-ÿ]+)\s+\"s\b", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\b([A-Za-zÀ-ÿ]+)\s+'s\b", r"\1", text, flags=re.IGNORECASE)

    if text.count('"') % 2 == 1:
        text = text.replace('"', "'", 1)

    while text.count(")") > text.count("("):
        idx = text.rfind(")")
        if idx == -1:
            break
        text = text[:idx] + text[idx + 1 :]

    while text.count("(") > text.count(")"):
        idx = text.rfind("(")
        if idx == -1:
            break
        text = text[:idx] + text[idx + 1 :]

    return text.strip()


def post_edit_english(text: str) -> str:
    text = _fix_common_spacing_and_encoding(text)

    for pat, rep in _PORTUGUESE_LEAK_REPLACEMENTS.items():
        text = re.sub(pat, rep, text)

    text = re.sub(
        r"\b[Pp]ote\s+de\s+[Dd]oces\s+da\s+([A-Z][A-Za-zÀ-ÿ']+)\b",
        r"\1's candy jar",
        text,
    )

    if text.count('"') % 2 == 1:
        text = text.replace('"', "'", 1)

    while text.count(")") > text.count("("):
        idx = text.rfind(")")
        if idx == -1:
            break
        text = text[:idx] + text[idx + 1 :]

    while text.count("(") > text.count(")"):
        idx = text.rfind("(")
        if idx == -1:
            break
        text = text[:idx] + text[idx + 1 :]

    return text.strip()


def _fix_common_spacing_and_encoding(text: str) -> str:
    text = (
        text.replace("â€œ", '"')
        .replace("â€\x9d", '"')
        .replace("â€˜", "'")
        .replace("â€™", "'")
        .replace("â€", '"')
        .replace("â\x80\x99", "'")
        .replace("Â", "")
    )
    text = re.sub(r"\b[Tt][Mm]\b", "", text)
    text = re.sub(r"^\.\s+\.\s+\(([^)]+)\)", r". (\1)", text)
    text = re.sub(r"\.\s*\.\s*\.", "...", text)
    text = re.sub(r"\.\.\.+", "...", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([\"'])\s+([,.;:!?])", r"\1\2", text)
    text = re.sub(r"([,.;:!?])\s+([\"'])", r"\1\2", text)
    text = re.sub(r"([?!])\s+([?!])", r"\1\2", text)
    text = re.sub(r"([,;:!?])(?![\s\])}\"'\d,.;:!?])", r"\1 ", text)
    text = re.sub(r"(?<!\.)\.(?![.\s\])}\"'\d,;:!?])", ". ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text


def _replace_slide_contextual(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        low = token.lower()
        window = text[max(0, match.start() - 50) : min(len(text), match.end() + 50)].lower()

        if any(ctx in window for ctx in _SLIDE_PRESENTATION_CONTEXT):
            return token
        if any(ctx in window for ctx in _SLIDE_PLAYGROUND_CONTEXT):
            return "escorregadores" if low == "slides" else "escorregador"
        return token

    return re.sub(r"\bslides?\b", repl, text, flags=re.IGNORECASE)


def _normalize_ptpt_to_ptbr(text: str) -> str:
    for pat, repl in _PTPT_TO_PTBR_PHRASE_REPLACEMENTS:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    for pat, repl in _PTPT_TO_PTBR_TOKEN_REPLACEMENTS:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    return text


def postprocess(text: str, direction: str) -> str:
    if direction == "en-pt":
        return post_edit_portuguese(text)
    if direction == "pt-en":
        return post_edit_english(text)
    raise ValueError(f"Unsupported direction: {direction}")
