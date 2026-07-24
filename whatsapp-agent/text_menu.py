"""Interpretação tolerante de respostas por texto livre em menus numerados —
usado pelo motor de agendamento (booking_flow.py) e pelo construtor de fluxo
configurável (flow_engine.py). Extraído de booking_flow.py pra não duplicar:
os dois módulos precisam da mesma tolerância a erro de digitação (quem está
do outro lado pode estar com pressa, nervoso ou com dificuldade de digitar).
"""

import difflib
import unicodedata

NUMBER_WORDS = {
    "um": 1, "uma": 1, "primeiro": 1, "primeira": 1,
    "dois": 2, "segundo": 2, "segunda": 2,
    "tres": 3, "terceiro": 3, "terceira": 3,
    "quatro": 4, "quarto": 4, "quarta": 4,
    "cinco": 5, "quinto": 5, "quinta": 5,
    "seis": 6, "sexto": 6, "sexta": 6,
    "sete": 7, "setimo": 7, "setima": 7,
    "oito": 8, "oitavo": 8, "oitava": 8,
    "nove": 9, "nono": 9, "nona": 9,
    "dez": 10, "decimo": 10, "decima": 10,
}


def normalize(text):
    """Minúsculo, sem acento, sem espaço nas pontas — base pra toda
    comparação de texto tolerante a erro deste módulo."""
    t = (text or "").strip().lower()
    t = unicodedata.normalize("NFKD", t)
    return "".join(ch for ch in t if not unicodedata.combining(ch))


def fuzzy_match(text, candidates, cutoff=0.75):
    """Compara text (já normalizado) contra uma lista de palavras/frases-alvo,
    tolerando pequeno erro de digitação (1-2 letras trocadas/faltando).
    Devolve o candidato mais parecido ou None."""
    if not text or not candidates:
        return None
    matches = difflib.get_close_matches(text, candidates, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def matches_any(text, phrases=(), words=()):
    t = normalize(text)
    if any(p in t for p in phrases):
        return True
    if not words:
        return False
    tokens = t.split()
    return any(fuzzy_match(tok, words) for tok in tokens)


def parse_index(text, options, names=None):
    """Fallback de texto pra resposta de lista — 'digite o número da opção'.
    Aceita o dígito puro (com pontuação/espaço ao redor, ex: '1)', '1.'), por
    extenso ('um', 'primeiro'...), e — quando names é passado (lista de nomes
    na mesma ordem de options) — o nome digitado, mesmo com erro de
    digitação, via correspondência aproximada. 1-based na mensagem, devolve
    o índice 0-based em options."""
    if not options:
        return None
    t = normalize(text).strip(" .)-")
    if t.isdigit():
        i = int(t) - 1
        return i if 0 <= i < len(options) else None
    if t in NUMBER_WORDS:
        i = NUMBER_WORDS[t] - 1
        return i if 0 <= i < len(options) else None
    if names:
        normalized_names = [normalize(n) for n in names]
        # nome digitado parcial (ex: só o primeiro nome) bate por substring
        # antes de tentar aproximação por erro de digitação
        for i, n in enumerate(normalized_names):
            if t and n and (t in n or n in t):
                return i
        match = fuzzy_match(t, [n for n in normalized_names if n], cutoff=0.6)
        if match is not None:
            return normalized_names.index(match)
    return None
