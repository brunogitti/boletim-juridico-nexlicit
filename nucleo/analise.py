"""nucleo/analise.py — Camada 5: estágio 2, análise completa das decisões
já triadas como relevantes. Recebe o texto completo do item (diferente do
trecho barato da Camada 4). Prompt e schema reproduzem os 6 pontos e as 3
proibições de docs/ARQUITETURA.md literalmente.
"""

import re
from dataclasses import dataclass

from nucleo.llm import ClienteLLM

INSTRUCOES_ANALISE = """\
Você recebe o texto completo de uma decisão de tribunal de contas ou
tribunal superior brasileiro, já triada como relevante para um consultor
que assessora EMPRESAS FORNECEDORAS que vendem para o setor público sob a
Lei 14.133/2021.

Analise o texto e responda:
1. O que foi decidido (2 a 3 frases).
2. Fundamentação legal: quais artigos da Lei 14.133/2021 são citados no texto.
3. Impacto prático para o fornecedor.
4. O que muda no procedimento de quem presta consultoria.
5. Nível de impacto: alto, medio ou baixo.
6. Resumo final de no máximo 200 palavras, juntando os pontos acima.

Regras obrigatórias:
- Todo campo tem que sair do texto fornecido. Não havendo base, retorne null.
- Proibido afirmar que o entendimento é novo, inédito, ou que muda
  jurisprudência anterior. Não existe base histórica para sustentar isso
  nesta fase. Se o próprio texto da fonte disser que é mudança de
  entendimento, você pode reproduzir isso citando que a fonte afirma.
- Proibido citar artigo de lei que não apareça no texto.
"""

# Só "resumo" é obrigatório — mesmo motivo do schema da triagem (Etapa 5):
# {"type": ["string", "null"]} é inconsistente na prática em
# response_json_schema do Gemini; campo ausente do JSON de saída já
# resolve "sem base = null" sem depender disso. Os pontos 1/3/4 do prompt
# ("o que foi decidido", "impacto prático", "o que muda no procedimento")
# entram como campos de raciocínio explícito — ajudam o modelo a chegar
# num resumo melhor — mas só "resumo" (ponto 6, síntese dos três) tem
# coluna própria em `decisoes` (Etapa 1); os outros dois campos
# estruturados (artigos_lei, impacto) também têm coluna e são gravados.
SCHEMA_ANALISE = {
    "type": "object",
    "properties": {
        "o_que_foi_decidido": {"type": "string"},
        "artigos_lei": {"type": "array", "items": {"type": "string"}},
        "impacto_pratico": {"type": "string"},
        "mudanca_procedimento": {"type": "string"},
        "impacto": {"type": "string", "enum": ["alto", "medio", "baixo"]},
        "resumo": {"type": "string"},
    },
    "required": ["resumo"],
}


@dataclass
class ResultadoAnalise:
    o_que_foi_decidido: str | None
    artigos_lei: list[str] | None
    impacto_pratico: str | None
    mudanca_procedimento: str | None
    impacto: str | None
    resumo: str | None


def analisar(cliente: ClienteLLM, *, titulo: str, texto_completo: str) -> ResultadoAnalise:
    """Chama a análise estágio 2 sobre título + texto completo da decisão."""
    entrada = f"Título: {titulo}\n\n{texto_completo}"
    dados = cliente.gerar_json(
        instrucoes=INSTRUCOES_ANALISE, entrada=entrada, schema=SCHEMA_ANALISE,
    )
    artigos = dados.get("artigos_lei")
    return ResultadoAnalise(
        o_que_foi_decidido=dados.get("o_que_foi_decidido"),
        artigos_lei=list(artigos) if artigos else None,
        impacto_pratico=dados.get("impacto_pratico"),
        mudanca_procedimento=dados.get("mudanca_procedimento"),
        impacto=dados.get("impacto"),
        resumo=dados.get("resumo"),
    )


def tem_ancora(*, numero_acordao: str | None, numero_processo: str | None,
                url_inteiro_teor: str | None) -> bool:
    """Regra não-negociável (CLAUDE.md): "nenhuma decisão entra no e-mail
    sem número de acórdão E link para a fonte original" — os dois são
    exigidos, falta de qualquer um dos dois barra o item.

    "Número" aqui é acórdão OU processo, o que a fonte usar pra se citar
    (achado real: TCE-SP, STJ e TCE-MG decisão própria nunca têm número
    de acórdão, só de processo — são 63% das decisões relevantes já no
    banco; exigir acórdão ao pé da letra excluiria a maioria do boletim,
    o que não é a intenção da regra).

    Sem essas duas informações, `triagem_status` vira 'sem_ancora' — fica
    registrado, mas não entra no e-mail."""
    tem_numero = bool(numero_acordao or numero_processo)
    tem_link = bool(url_inteiro_teor)
    return tem_numero and tem_link


# Achado real (2026-08-11): duas decisões da Zênite viraram sem_ancora com
# numero_acordao e numero_processo None — não porque a notícia não cite
# nenhum dos dois, mas porque o número só aparece num rodapé "Serviço" no
# fim do artigo (~7500 caracteres), muito depois do trecho de 1200
# caracteres que a triagem (Camada 4) vê. O rodapé segue um padrão fixo:
# "Processo(s) nº:\n<valor>" e "Acórdão(s) nº:\n<valor>". A Análise
# (Camada 5) já tem o texto completo em mãos por causa do refatiamento —
# tenta esse padrão aqui antes de aceitar sem_ancora como veredito final,
# sem mexer no limite de 1200 caracteres da triagem (que continua barato
# de propósito, esse fallback só roda nos poucos itens que já iam ficar
# sem âncora mesmo).
_PADRAO_RODAPE_ACORDAO = re.compile(
    r"Ac[oó]rd[aã]os?\s*n[ºo°.]{1,2}\s*:\s*\n\s*(\d+/\d+)", re.IGNORECASE,
)
_PADRAO_RODAPE_PROCESSO = re.compile(
    r"Processos?\s*n[ºo°.]{1,2}\s*:\s*\n\s*([^\n]+)", re.IGNORECASE,
)


def recuperar_ancora_do_texto(texto_completo: str) -> tuple[str | None, str | None]:
    """Procura "Processo(s) nº:" / "Acórdão(s) nº:" no texto completo.
    Devolve (numero_acordao, numero_processo) — qualquer um dos dois pode
    vir None se não achar. Não mexe em url_inteiro_teor: isso não precisa
    ser extraído do texto, e se estivesse faltando o fallback não
    ajudaria mesmo (ver tem_ancora)."""
    m_acordao = _PADRAO_RODAPE_ACORDAO.search(texto_completo)
    m_processo = _PADRAO_RODAPE_PROCESSO.search(texto_completo)
    numero_acordao = m_acordao.group(1).strip() if m_acordao else None
    numero_processo = m_processo.group(1).strip() if m_processo else None
    return numero_acordao, numero_processo
