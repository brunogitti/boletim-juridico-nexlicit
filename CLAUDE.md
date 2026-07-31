# Boletim Jurídico NexLicit

Sistema que envia um e-mail diário às 7h (Brasília) com decisões e entendimentos
sobre a Lei 14.133/2021, coletados de tribunais e de fonte especializada,
triados por relevância para o FORNECEDOR que vende ao setor público.

**A arquitetura completa está em `docs/ARQUITETURA.md`. Leia esse arquivo antes
de planejar qualquer etapa. Ele define fontes, schema, prompts e as regras de
extração.**

## Como trabalhar comigo

- Uma etapa por vez. Nunca avance para a próxima sem eu aprovar explicitamente.
- Explique antes de aplicar. Não execute mudança sem me mostrar o que vai fazer.
- Nada de código mágico. Se precisar de uma abstração, justifique.
- Não invente API, endpoint, parâmetro ou formato de retorno. Se não tem certeza,
  busque a documentação ou me pergunte.
- Verifique premissa contra dado real. Antes de escrever um parser de HTML,
  busque a página e olhe a estrutura de verdade.
- Sinalize risco antes de implementar, não depois.

## Convenções de código

- Python. Comentários, docstrings e nomes de identificador em português.
- Termos do domínio ficam em português mesmo quando soam estranhos:
  `numero_acordao`, `orgao_julgador`, `chave_dedup`, `triagem_status`.
- SQLite com `sqlite3` puro, sem ORM. Toda escrita dentro de transação.
- Sem dependência nova sem me perguntar antes.
- Todo coletor falha isolado. Erro em uma fonte não pode derrubar o job inteiro.
- Log estruturado. Falha silenciosa é pior que falha barulhenta.

## Regras que não se negociam

- **Nenhuma decisão entra no e-mail sem número de acórdão E link para a fonte
  original.** Sem âncora, o item vai para `triagem_status = 'sem_ancora'` e fica
  registrado, mas não é enviado.
- **O LLM não pode afirmar que um entendimento é novo, inédito ou que muda
  jurisprudência anterior.** Não existe base histórica para sustentar isso.
  Se a própria fonte afirmar, pode reproduzir atribuindo à fonte.
- **O LLM não pode citar artigo de lei que não apareça no texto analisado.**
- Campo sem base no texto retorna `null`, nunca um palpite.
- Dedup é por `tribunal + numero_acordao + ano`, nunca por URL. A mesma decisão
  aparece em fontes diferentes.
- Se não houver decisão relevante no dia, o job encerra sem enviar e-mail.

## Custo

Tudo roda em free tier. Gemini Flash com `temperature=0.1`,
`thinking_level="low"` e JSON schema nativo. Triagem em dois estágios: o estágio
1 filtra em cima de título e ementa, só o que passa vai para análise completa.
Processar em lote sequencial, nunca em paralelo, com backoff exponencial.

## Segredos

Credenciais em `.env` (gitignored) no local e em secrets do GitHub no CI.
Editais e documentos de cliente nunca entram no repositório.
O banco SQLite vive na branch `data`, separada, para não poluir o histórico.
