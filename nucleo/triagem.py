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

Notícia institucional sobre lançamento de ferramenta, disponibilização de
material de apoio, ou anúncio de funcionalidade NÃO é decisão nem
entendimento jurídico, mesmo que mencione a Lei 14.133/2021 ou algum tema
da lista de relevância acima — marque como NÃO relevante.

Na dúvida entre relevante e não relevante, marque como NÃO relevante.
É melhor perder uma decisão do que poluir o boletim.

Extraia apenas o que estiver LITERALMENTE no texto. Campo ausente = null.
Nunca invente número de acórdão, relator ou data.

Se o texto citar um número de acórdão de verdade (decisão já julgada),
preencha numero_acordao. Se for notícia sobre licitação ainda em
andamento (sem decisão julgada), sem número de acórdão, mas citar o
número do próprio instrumento — Concorrência, Pregão, Edital, Resolução
— preencha numero_identificador com esse número, e deixe numero_acordao
null. Nunca preencha os dois com o mesmo valor.
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
        "numero_identificador": {"type": "string"},
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
    numero_identificador: str | None  # Concorrência/Pregão/Edital/Resolução —
    # licitação em andamento, sem acórdão julgado ainda (achado real 2026-08-11)
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
        tema=_valor_ou_none(dados.get("tema")),
        tribunal=_valor_ou_none(dados.get("tribunal")),
        numero_acordao=_valor_ou_none(dados.get("numero_acordao")),
        numero_identificador=_valor_ou_none(dados.get("numero_identificador")),
        relator=_valor_ou_none(dados.get("relator")),
        data_julgamento=_valor_ou_none(dados.get("data_julgamento")),
        impacto_estimado=_valor_ou_none(dados.get("impacto_estimado")),
    )


def _valor_ou_none(valor: str | None) -> str | None:
    """Achado real (2026-08-10): o modelo às vezes devolve a string
    literal "null" (4 caracteres) pra um campo fora de "required", em vez
    de simplesmente omitir a chave. Trata isso e string vazia como
    ausência de valor de verdade."""
    if valor is None or valor.strip().lower() in ("", "null"):
        return None
    return valor


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
    devolveu.

    O gate é por `decisao.tribunal`, não campo a campo. Achado real
    (2026-08-10): campo a campo, `numero_acordao=None` do TCE-SP e do
    TCE-MG (decisão própria) — que é o fatiador dizendo com confiança que
    essa fonte não cita por acórdão, só por processo — virava "faltou
    preencher", e o LLM enchia com o próprio número de processo (o único
    número visível no texto), duplicando o valor no campo errado. 63% das
    decisões relevantes do banco saíram assim. O LLM só é fonte de verdade
    pra esses campos quando o fatiador não tem *nenhuma* informação
    estrutural pra decisão inteira — hoje, só a Zênite
    (`decisao.tribunal is None`). Fora isso, os campos vêm do fatiador
    tal como estão, mesmo quando algum vier `None` individualmente.

    `numero_processo` reaproveita a coluna existente pra também guardar
    número de edital/Concorrência/Pregão/Resolução (achado real
    2026-08-11: notícia da Zênite sobre licitação em andamento, sem
    acórdão julgado, cita o número do próprio instrumento logo no primeiro
    parágrafo — dentro do trecho de 1200 caracteres da triagem, então não
    precisa do fallback de texto completo). Mesmo padrão já usado pro
    número de súmula do TCE-SP reaproveitando `numero_acordao` — não cria
    coluna nova pra isso, `tem_ancora()` (nucleo/analise.py) já aceita
    qualquer coisa em `numero_processo` como identificador válido.

    `tribunal` é a única coluna NOT NULL nesse grupo: se nem o fatiador nem
    a triagem conseguirem identificá-lo (notícia da Zênite sem tribunal
    específico), grava TRIBUNAL_NAO_IDENTIFICADO em vez de quebrar o
    INSERT — não é um palpite sobre qual tribunal é, só registra
    honestamente que não deu pra saber (decisão combinada antes de
    implementar)."""
    if decisao.tribunal is None:
        return {
            "tribunal": resultado.tribunal or TRIBUNAL_NAO_IDENTIFICADO,
            "numero_acordao": resultado.numero_acordao,
            "numero_processo": resultado.numero_identificador,
            "relator": resultado.relator,
            "data_julgamento": resultado.data_julgamento,
        }
    return {
        "tribunal": decisao.tribunal,
        "numero_acordao": decisao.numero_acordao,
        "numero_processo": decisao.numero_processo,
        "relator": decisao.relator,
        "data_julgamento": decisao.data_julgamento,
    }
