"""nucleo/triagem.py — Camada 4: estágio 1 do crivo de relevância.

Roda sobre título + primeiro parágrafo (ementa), não o texto inteiro — isso
é trabalho da Camada 5 (Etapa 6), só para o que passar aqui. Prompt e
schema são os de docs/ARQUITETURA.md, reproduzidos literalmente.
"""

import re
from dataclasses import dataclass

from nucleo.fatiador import DecisaoFatiada
from nucleo.llm import ClienteLLM

TRIBUNAL_NAO_IDENTIFICADO = "Não identificado"

TAMANHO_MAXIMO_TRECHO = 1200  # caracteres; teto de segurança de custo

INSTRUCOES_TRIAGEM = """\
Você recebe o trecho inicial de uma decisão ou notícia de tribunal de contas
ou tribunal superior brasileiro.

Responda se ela é relevante para um consultor que assessora EMPRESAS
FORNECEDORAS que vendem para o setor público sob a Lei 14.133/2021.

É RELEVANTE se tratar de: habilitação (jurídica, fiscal, técnica,
econômico-financeira), atestado de capacidade técnica, balanço patrimonial,
ME/EPP e LC 123, diligência, desclassificação de proposta, recurso
administrativo, intenção de recurso, sanções a licitantes, registro de preços,
dispensa eletrônica, pregão eletrônico, pesquisa de preços, reequilíbrio,
execução e fiscalização contratual, garantia contratual, subcontratação,
consórcios, impugnação, pedido de esclarecimento, ou exigências de edital.

NÃO é relevante se tratar apenas de: pessoal, aposentadoria, pensão, concurso
público, contas de governo, obrigações do gestor sem reflexo para o licitante,
prescrição de sanção a gestor, improbidade, ou processos internos do tribunal.

Na dúvida entre relevante e não relevante, marque como NÃO relevante.
É melhor perder uma decisão do que poluir o boletim.

Extraia apenas o que estiver LITERALMENTE no texto. Campo ausente = null.
Nunca invente número de acórdão, relator ou data.
"""

# Só "relevante" e "motivo" são obrigatórios. Os demais ficam de fora de
# "required" de propósito: documentação oficial do Gemini registra que
# {"type": ["string", "null"]} é inconsistente na prática em JSON Schema
# nativo (response_json_schema); campo opcional ausente do JSON de saída
# tem o mesmo efeito de "null" sem depender desse comportamento.
SCHEMA_TRIAGEM = {
    "type": "object",
    "properties": {
        "relevante": {"type": "boolean"},
        "motivo": {"type": "string"},
        "tema": {"type": "string"},
        "tribunal": {"type": "string"},
        "numero_acordao": {"type": "string"},
        "relator": {"type": "string"},
        "data_julgamento": {"type": "string"},
        "impacto_estimado": {"type": "string", "enum": ["alto", "medio", "baixo"]},
    },
    "required": ["relevante", "motivo"],
}


@dataclass
class ResultadoTriagem:
    relevante: bool
    motivo: str
    tema: str | None
    tribunal: str | None
    numero_acordao: str | None
    relator: str | None
    data_julgamento: str | None
    impacto_estimado: str | None


def triar(cliente: ClienteLLM, *, titulo: str, trecho: str) -> ResultadoTriagem:
    """Chama a triagem estágio 1 sobre título + trecho de uma decisão."""
    entrada = f"Título: {titulo}\n\n{trecho}"
    dados = cliente.gerar_json(
        instrucoes=INSTRUCOES_TRIAGEM, entrada=entrada, schema=SCHEMA_TRIAGEM,
    )
    return ResultadoTriagem(
        relevante=bool(dados["relevante"]),
        motivo=str(dados["motivo"]),
        tema=dados.get("tema"),
        tribunal=dados.get("tribunal"),
        numero_acordao=dados.get("numero_acordao"),
        relator=dados.get("relator"),
        data_julgamento=dados.get("data_julgamento"),
        impacto_estimado=dados.get("impacto_estimado"),
    )


_PADRAO_SECAO_SUBSTANTIVA = re.compile(
    r"\n\n(?:Ementa|Tema):\s*\n?(.*?)(?=\n\n|\Z)", re.DOTALL,
)


def extrair_trecho(texto_decisao: str, *, limite: int = TAMANHO_MAXIMO_TRECHO) -> str:
    """Título + ementa (o que a Camada 4 pede), cortado num teto de
    caracteres por segurança de custo.

    Achado real (2026-08-07): pra TCU e STJ, o "primeiro parágrafo" (texto
    até o primeiro '\\n\\n') é só o título — a Citação/Ementa real do TCU e
    o Tema real do STJ vêm em parágrafos seguintes, e com esse texto a
    triagem nunca via conteúdo nenhum pra avaliar (100% descartado nas
    duas fontes, todo motivo dizendo "só tem o título"). Quando existe uma
    seção rotulada "Ementa:" ou "Tema:", ela entra depois do título. Fontes
    sem esse rótulo (TCE-PR, TCE-SP, TCE-MG, Zênite) continuam no
    comportamento antigo — pegar o primeiro parágrafo inteiro, sem regressão."""
    titulo = texto_decisao.split("\n\n", 1)[0].strip()

    m = _PADRAO_SECAO_SUBSTANTIVA.search(texto_decisao)
    if m:
        trecho = f"{titulo}\n\n{m.group(1).strip()}"
    else:
        trecho = titulo

    return trecho[:limite]


def mesclar_metadados(decisao: DecisaoFatiada, resultado: ResultadoTriagem) -> dict:
    """Resolve os campos de metadado entre o que o fatiador já extraiu por
    regex (linha de citação formal — mais confiável) e o que a triagem
    devolveu. O LLM só é usado onde o fatiador deixou o campo em None de
    propósito (hoje, só a Zênite); nunca sobrescreve um valor
    determinístico já extraído.

    `tribunal` é a única coluna NOT NULL nesse grupo: se nem o fatiador nem
    a triagem conseguirem identificá-lo (notícia da Zênite sem tribunal
    específico), grava TRIBUNAL_NAO_IDENTIFICADO em vez de quebrar o
    INSERT — não é um palpite sobre qual tribunal é, só registra
    honestamente que não deu pra saber (decisão combinada antes de
    implementar)."""
    return {
        "tribunal": decisao.tribunal or resultado.tribunal or TRIBUNAL_NAO_IDENTIFICADO,
        "numero_acordao": decisao.numero_acordao or resultado.numero_acordao,
        "relator": decisao.relator or resultado.relator,
        "data_julgamento": decisao.data_julgamento or resultado.data_julgamento,
    }
