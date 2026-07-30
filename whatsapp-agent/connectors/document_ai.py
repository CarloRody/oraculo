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

DEFAULT_CHRONOLOGICAL_PROMPT_HEADER = (
    "Você é um assistente médico. Abaixo estão os resumos de vários "
    "documentos do MESMO paciente, cada um já processado individualmente, "
    "listados em ordem cronológica. Alguns documentos podem ser páginas ou "
    "mensagens separadas do mesmo laudo/exame, então pode haver informação "
    "repetida entre eles.\n\n"
    "Sua tarefa: escrever UM resumo cronológico único, em português, "
    "organizando a evolução do paciente ao longo do tempo, SEM repetir a "
    "mesma informação mais de uma vez — quando dois ou mais documentos "
    "claramente pertencem ao mesmo laudo/exame (datas próximas, mesmo tipo "
    "de conteúdo), trate-os como uma única entrada na linha do tempo. "
    "Destaque tendências, mudanças e alertas relevantes entre os "
    "documentos.\n\nDocumentos (mais antigo primeiro):\n"
)


class DocumentAIError(Exception):
    pass


def _chat(content, max_tokens):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
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


def summarize_document(images, prompt=None):
    """images: lista de (bytes, mime_type), uma entrada por página/imagem.
    Retorna (texto_resumo, prompt_tokens, completion_tokens, elapsed_seconds)."""
    content = [{"type": "text", "text": prompt or DEFAULT_SUMMARY_PROMPT}]
    for raw_bytes, mime_type in images:
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}})
    return _chat(content, max_tokens=800)


def summarize_chronological(prompt, document_count=1):
    """prompt: texto já pronto (ver document_summary._build_chronological_prompt)
    — só texto, sem imagem, já que a entrada são os resumos individuais já
    gerados, não os documentos originais de novo. Mesmo formato de retorno de
    summarize_document.

    `document_count` dimensiona max_tokens — um teto fixo baixo (era 1500)
    cortava a resposta no meio pra pacientes com muitos documentos: o
    completion_tokens batia exatamente no teto e o texto parava ali, sem
    aviso, dando a impressão de que o resumo "perdeu" informação. ~350
    tokens por documento dá folga pra cada entrada da linha do tempo, com
    piso de 1500 (paciente com poucos documentos) e teto de 8000 (evita
    pedido de tamanho desproporcional)."""
    max_tokens = max(1500, min(8000, document_count * 350 + 500))
    return _chat([{"type": "text", "text": prompt}], max_tokens=max_tokens)
