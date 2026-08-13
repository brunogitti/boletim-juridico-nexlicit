"""nucleo/llm.py — camada trocável de cliente LLM.

`ClienteLLM` é a interface que o resto do pipeline (nucleo/triagem.py, e
depois a Etapa 6) usa; `ClienteGeminiFlash` é a implementação padrão. Trocar
de provedor no futuro (outro modelo, outra nuvem) significa escrever uma
nova subclasse e apontar LLM_PROVIDER pra ela — quem chama não muda.

API real do pacote `google-genai` (confirmada contra a documentação oficial
em agosto/2026, não a "Interactions API" nova — que é pra agentes com
estado, sobra de abstração pro que a gente precisa aqui):
`client.models.generate_content(model=..., contents=..., config=types.GenerateContentConfig(...))`.
`thinking_level` mora dentro de `types.ThinkingConfig`, não solto na config.
`response_json_schema` (não `response_schema`) é o campo que aceita JSON
Schema puro em vez de exigir `genai.types.Schema`/Pydantic — é isso que
docs/ARQUITETURA.md chama de "schema nativo". Erros da API chegam como
`google.genai.errors.ClientError` (4xx) ou `ServerError` (5xx), ambos com
atributo `.code` (status HTTP) — usado abaixo pra decidir o que é
transitório (429, 5xx) e o que é erro de configuração (não adianta repetir).
"""

import json
import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod

from google import genai
from google.genai import errors, types

logger = logging.getLogger(__name__)

MODELO_PADRAO = "gemini-3.5-flash-lite"
TEMPERATURE = 0.1
THINKING_LEVEL = "low"
TENTATIVAS_MAX = 5
ESPERA_INICIAL = 1.0  # segundos; dobra a cada nova tentativa, mais jitter


class ErroLLM(Exception):
    """Erro definitivo do cliente LLM — todas as tentativas de backoff se
    esgotaram, ou o erro não era transitório (não adianta repetir)."""


class ErroConfiguracaoLLM(ErroLLM):
    """Variável de ambiente obrigatória ausente ou provedor desconhecido."""


class ClienteLLM(ABC):
    """Interface trocável: quem consome só conhece este contrato."""

    @abstractmethod
    def gerar_json(self, *, instrucoes: str, entrada: str, schema: dict) -> dict:
        """Chama o modelo e devolve o JSON já parseado (validado contra
        `schema` pelo próprio provedor, via saída estruturada nativa).

        Levanta ErroLLM se todas as tentativas de backoff se esgotarem, ou
        na hora se o erro não for transitório (4xx que não seja 429)."""


class ClienteGeminiFlash(ClienteLLM):
    """Implementação padrão: Gemini Flash via `google-genai`."""

    def __init__(self, *, chave_api: str, modelo: str = MODELO_PADRAO,
                 tentativas_max: int = TENTATIVAS_MAX,
                 espera_inicial: float = ESPERA_INICIAL):
        self._cliente = genai.Client(api_key=chave_api)
        self._modelo = modelo
        self._tentativas_max = tentativas_max
        self._espera_inicial = espera_inicial

    def gerar_json(self, *, instrucoes: str, entrada: str, schema: dict) -> dict:
        config = types.GenerateContentConfig(
            system_instruction=instrucoes,
            temperature=TEMPERATURE,
            thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
            response_mime_type="application/json",
            response_json_schema=schema,
        )

        ultimo_erro: Exception | None = None
        for tentativa in range(self._tentativas_max):
            try:
                resposta = self._cliente.models.generate_content(
                    model=self._modelo, contents=entrada, config=config,
                )
                if not resposta.text:
                    raise ErroLLM("Gemini devolveu resposta vazia (possível bloqueio de segurança)")
                return json.loads(resposta.text)
            except errors.ServerError as erro:
                ultimo_erro = erro
                logger.warning(
                    "erro do servidor Gemini, tentando de novo",
                    extra={"tentativa": tentativa, "codigo": erro.code},
                )
            except errors.ClientError as erro:
                if erro.code != 429:
                    # erro de configuração (400 schema inválido, 403 chave
                    # errada etc.) — repetir não resolve, falha na hora
                    raise ErroLLM(f"erro não transitório do Gemini: {erro}") from erro
                ultimo_erro = erro
                logger.warning(
                    "limite de taxa do Gemini (429), tentando de novo",
                    extra={"tentativa": tentativa},
                )
            except json.JSONDecodeError as erro:
                raise ErroLLM(f"Gemini devolveu JSON inválido: {erro}") from erro

            if tentativa < self._tentativas_max - 1:
                espera = self._espera_inicial * (2 ** tentativa) + random.uniform(0, 0.5)
                time.sleep(espera)

        raise ErroLLM(
            f"Gemini falhou após {self._tentativas_max} tentativas: {ultimo_erro}"
        ) from ultimo_erro


def criar_cliente_llm() -> ClienteLLM:
    """Lê a configuração do ambiente (LLM_PROVIDER, GEMINI_API_KEY,
    GEMINI_MODEL) e devolve a implementação certa. Só "gemini" existe hoje
    — é o ponto de extensão, não uma fábrica genérica."""
    provedor = os.environ.get("LLM_PROVIDER", "gemini")
    if provedor != "gemini":
        raise ErroConfiguracaoLLM(
            f"LLM_PROVIDER desconhecido: {provedor!r} (só 'gemini' implementado)"
        )

    chave_api = os.environ.get("GEMINI_API_KEY")
    if not chave_api:
        raise ErroConfiguracaoLLM("GEMINI_API_KEY não configurada no ambiente")

    modelo = os.environ.get("GEMINI_MODEL") or MODELO_PADRAO
    return ClienteGeminiFlash(chave_api=chave_api, modelo=modelo)


# Achado real: o modelo às vezes devolve uma string-placeholder pra um
# campo opcional em vez de simplesmente omitir a chave — e cada rodada
# inventa uma variante diferente da mesma ideia ("null" em 2026-08-10,
# "__NULL__" em 2026-08-13; "(ausente)" apareceu numa decisão sem se
# confirmar como vindo do pipeline). Em vez de colecionar strings exatas
# uma a uma, casa qualquer coisa que seja só pontuação/sublinhado em
# volta de uma palavra-símbolo de ausência conhecida.
_PADRAO_VALOR_NULO = re.compile(
    r"^[_\-\s()]*(null|none|n/?a|ausente|indispon[íi]vel)[_\-\s()]*$", re.IGNORECASE,
)


def valor_ou_none(valor: str | None) -> str | None:
    """Trata string vazia ou placeholder de ausência (ver _PADRAO_VALOR_NULO)
    como None de verdade. Compartilhado entre triagem e análise — mesmo
    tipo de saída de LLM (JSON com campo opcional fora de "required"),
    mesmo tratamento, pra não duplicar nem deixar um dos dois sem a
    proteção quando o modelo inventa mais uma variante."""
    if valor is None:
        return None
    limpo = valor.strip()
    if limpo == "" or _PADRAO_VALOR_NULO.match(limpo):
        return None
    return valor
