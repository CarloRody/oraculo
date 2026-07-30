"""Cliente REST do modelo de IA local com visão (LM Studio, mesma instância
já usada pelo ai_oraculo_saas pro chat — ver config.yaml, seção `llm`).

Formato de mensagem de visão confirmado ao vivo contra o servidor real
(192.168.25.8:1234): POST /v1/chat/completions, API compatível OpenAI,
imagem em `content` como `{"type": "image_url", "image_url": {"url":
"data:<mime>;base64,<...>"}}`. A resposta traz `usage.completion_tokens`
exato — não precisa estimar tokens por caractere. O campo `stats` do LM
Studio veio vazio no teste, então tokens/segundo é calculado por quem chama
esta função, dividindo completion_tokens pelo tempo decorrido da requisição.

Este módulo só fala HTTP com o modelo — nenhuma regra de negócio ou acesso a
banco aqui (isso fica em document_summary.py).
"""

import base64
import time

import requests

from config import LLM_CONFIG

BASE_URL = (LLM_CONFIG.get("base_url") or "http://192.168.25.8:1234/v1/chat/completions").rstrip("/")
MODEL = LLM_CONFIG.get("model") or "auto"
TIMEOUT = LLM_CONFIG.get("timeout_seconds") or 600

DEFAULT_SUMMARY_PROMPT = (
    "Você está analisando um documento de paciente de uma clínica (exame, "
    "receita, laudo ou similar). Gere um resumo objetivo em português, "
    "destacando: o tipo de documento, os principais achados/valores "
    "relevantes, e qualquer recomendação ou alerta importante. Seja conciso "
    "(até um parágrafo curto). Se a imagem não for legível ou não parecer "
    "um documento médico, diga isso claramente em vez de inventar conteúdo."
)


class DocumentAIError(Exception):
    pass


def summarize_document(images, prompt=None):
    """images: lista de (bytes, mime_type), uma entrada por página/imagem.
    Retorna (texto_resumo, prompt_tokens, completion_tokens, elapsed_seconds)."""
    content = [{"type": "text", "text": prompt or DEFAULT_SUMMARY_PROMPT}]
    for raw_bytes, mime_type in images:
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}})

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 800,
    }

    start = time.monotonic()
    try:
        resp = requests.post(BASE_URL, json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise DocumentAIError(f"Não foi possível conectar ao modelo de IA em {BASE_URL} ({e})") from e
    except requests.exceptions.Timeout:
        raise DocumentAIError(f"Modelo de IA não respondeu em {TIMEOUT}s ({BASE_URL})")
    except requests.exceptions.RequestException as e:
        raise DocumentAIError(f"Falha ao chamar o modelo de IA: {e}") from e
    elapsed = time.monotonic() - start

    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
        prompt_tokens = data["usage"]["prompt_tokens"]
        completion_tokens = data["usage"]["completion_tokens"]
    except (KeyError, IndexError, TypeError) as e:
        raise DocumentAIError(f"Resposta inesperada do modelo de IA: {e} — corpo: {str(data)[:300]}") from e

    return text, prompt_tokens, completion_tokens, elapsed
