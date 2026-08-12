# Boletim Jurídico NexLicit — Documento de Arquitetura

**Repositório sugerido:** `boletim-juridico-nexlicit`
**Status:** planejamento fechado, pronto para implementação
**Data:** 30/07/2026

---

## 1. Objetivo

Um e-mail por dia, às 7h (horário de Brasília), com as decisões e entendimentos
sobre a Lei 14.133/2021 publicados desde o último envio, organizados por tribunal
e priorizados por impacto para o fornecedor.

**Regra de ouro:** se não houver nada relevante, não sai e-mail.

Não é um resumo de notícias. É a resposta para: *o que saiu que muda como eu
presto consultoria, participo de licitação ou oriento um cliente?*

### Fora de escopo (fase 1)

- Painel web, busca semântica, linha do tempo de entendimentos
- Alertas extraordinários fora do horário fixo
- Detecção automática de "isso contradiz decisão anterior" (volta na fase 3,
  quando existir base histórica de verdade)
- Instagram, LinkedIn e qualquer rede social
- Lei 13.303, convênios, terceiro setor, Sistema S, Lei 8.666

---

## 2. Fontes confirmadas

Todas foram testadas em 30/07/2026. Nada aqui é suposição.

| Fonte | Formato | Periodicidade real | Método |
|---|---|---|---|
| **Zênite** `zenite.com.br/noticias/` | WordPress, HTML server-side | Quase diária | RSS ou `wp-json`; fallback HTML |
| **TCU** — Informativo de Licitações e Contratos | PDF + Word | Quinzenal, terças | Scrape da listagem + PyMuPDF |
| **TCU** — Boletim de Jurisprudência | PDF + Word | Semanal | Idem, filtrar tema licitação |
| **TCE-SP** — Boletim de Jurisprudência + Súmulas | HTML/PDF | Mensal | Scrape da página do boletim |
| **TCE-MG** — Informativo de Jurisprudência | HTML puro | ~quinzenal | Scrape direto, sem PDF |
| **TCE-PR** — Boletim Informativo de Jurisprudência | HTML puro (+ PDF/DOCX) | ~semanal | Scrape direto, inteiro teor na página |
| **STJ** — Informativo de Jurisprudência | HTML/PDF periódico | ~quinzenal | Filtrar Direito Administrativo |

### Descartadas e por quê

- **JOTA** — SPA em Next.js, nada renderizado no servidor, e o conteúdo bom está
  atrás do paywall do JOTA PRO.
- **Sollicita** — SPA que exige JavaScript explicitamente. O endpoint interno
  provavelmente pede token de sessão, e é plataforma paga.
- **STJ Últimas Notícias** — SharePoint com renderização por JavaScript. A página
  volta praticamente vazia. Substituída pelo Informativo de Jurisprudência.
- **Instagram** — sem rota oficial para perfis de terceiros desde o fim da Basic
  Display API em dez/2024.

### Observações por fonte

**Zênite.** É o pulso diário do sistema. Publica quase todo dia e já varre vários
TCEs (SC, PR, SP apareceram na primeira página do teste). Sendo WordPress,
verificar nesta ordem: `/wp-json/wp/v2/posts?after=<ISO8601>` (ideal, JSON
estruturado com conteúdo completo), depois `/feed/` (RSS), depois raspar HTML.

**TCU.** A listagem em `portal.tcu.gov.br/jurisprudencia` renderiza no servidor,
mas o filtro `?tipos=` na URL não funciona (o portal é SPA em Next.js e o
parâmetro se perde). Raspar a listagem inteira e filtrar pelo texto do título no
código. Cada linha tem link para PDF e para Word.

**TCE-PR.** Melhor formato de todos. O inteiro teor vem na própria página HTML,
com a linha de citação no fim de cada item (`Acórdão n.º X/AAAA, Órgão, Rel.
NOME, julgado em DD/MM/AAAA`) e link para o ViaJuris. Extração de metadados por
regex é confiável aqui.

**TCE-MG.** HTML puro. Padrão de URL observado:
`tce.mg.gov.br/Informativo-de-Jurisprudencia-n-NNN.html/Noticia/ID` e
`tce.mg.gov.br/noticia/Detalhe/ID`. **Pendência de implementação:** localizar a
página índice que lista os informativos, para não depender de adivinhar o número
sequencial.

**Ética de coleta.** A Zênite é empresa privada. Uso pessoal, com intervalo entre
requisições, `User-Agent` identificado e sem republicação, é aceitável. Se o
boletim algum dia virar produto, isso muda e o caminho passa a ser assinar.

---

## 3. Arquitetura

Mesmo esqueleto do Radar NexLicit. Nada de novo além do necessário.

```
GitHub Actions (cron diário)
    │
    ├─ 1. COLETA        coletores/*.py  →  itens_brutos
    ├─ 2. FATIAMENTO    fatiador.py     →  decisões individuais
    ├─ 3. DEDUP         dedup.py        →  descarta o que já foi visto
    ├─ 4. TRIAGEM       Gemini Flash    →  relevante sim/não
    ├─ 5. ANÁLISE       Gemini Flash    →  resumo de 200 palavras
    ├─ 6. BOLETIM       montador.py     →  HTML do e-mail
    └─ 7. ENVIO         Gmail SMTP      →  só se houver conteúdo
```

### Agendamento

Cron do GitHub Actions em UTC. Brasil não tem mais horário de verão, então
UTC-3 é fixo.

```yaml
schedule:
  - cron: '37 9 * * *'   # 06:37 BRT
```

Minuto `:37` de propósito. O minuto `:00` é documentado pelo GitHub como o pior
horário de agendamento por congestionamento. Processamento leva de 5 a 15
minutos, então o e-mail cai entre 06:45 e 07:00.

### Camada 2 — Fatiamento

Um boletim do TCU é um PDF único com dezenas de decisões dentro. Mandar o
documento inteiro para o LLM produz resumo genérico. O fatiador quebra cada
publicação em itens individuais antes de qualquer chamada de IA.

Estratégias por fonte:

- **TCU (PDF):** PyMuPDF + regex nos marcadores de item numerado do boletim
- **TCE-PR (HTML):** split pelas âncoras de sumário / headings numerados
- **TCE-MG (HTML):** split por bloco de ementa
- **Zênite:** cada notícia já é um item, não precisa fatiar

### Camada 3 — Dedup

**A chave de dedup é o número do acórdão, não a URL.** O mesmo acórdão vai
aparecer na Zênite e no boletim do tribunal. Se a chave for a URL, você recebe a
mesma decisão duas vezes no mesmo e-mail.

```
chave = normalizar(f"{tribunal}|{numero_acordao}|{ano}")
```

Quando não houver número de acórdão extraível (notícia institucional, comunicado,
súmula nova), usar `sha256(titulo_normalizado + data_publicacao)` como fallback.

### Camada 4 — Triagem (estágio 1)

Roda sobre título + ementa/primeiro parágrafo. Barato, alto volume.
Saída em JSON com schema nativo. `temperature=0.1`, `thinking_level="low"`.

**Prompt de triagem:**

```
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

Notícia institucional sobre lançamento de FUNCIONALIDADE DE SISTEMA/PORTAL/
SITE (ex.: nova busca, nova área do site) ou disponibilização de MATERIAL DE
APOIO (manual, cartilha, FAQ) NÃO é decisão nem entendimento jurídico —
marque como NÃO relevante, mesmo que mencione a Lei 14.133/2021 ou algum
tema da lista de relevância acima.

Isso NÃO se aplica a decisão cautelar, acórdão, ou ato normativo (Resolução,
Portaria, Instrução Normativa) — mesmo quando o ato institui ou nomeia uma
plataforma/sistema (ex.: Resolução que cria um marketplace de compras).
Esses continuam avaliados pelos critérios de relevância normais acima.

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
```

**Schema de saída:**

```json
{
  "relevante": true,
  "motivo": "trata de exigência de atestado de capacidade técnica",
  "tema": "qualificacao_tecnica",
  "tribunal": "TCE-PR",
  "numero_acordao": "3190/2025",
  "numero_identificador": null,
  "relator": "THIAGO BARBOSA CORDEIRO",
  "data_julgamento": "2025-11-10",
  "impacto_estimado": "medio"
}
```

`numero_identificador` existe pra cobrir notícia de licitação em
andamento (achado real 2026-08-11, fonte Zênite): sem acórdão julgado
ainda, o único identificador citável do caso é o número do próprio
instrumento — Concorrência, Pregão, Edital, Resolução. Esse valor é
gravado na mesma coluna `numero_processo` de `decisoes` (ver Camada 6 e
`nucleo/triagem.mesclar_metadados` — mesmo padrão de reaproveitamento já
usado pro número de súmula do TCE-SP em `numero_acordao`; não existe
coluna `numero_edital` própria).

### Camada 5 — Análise (estágio 2)

Só o que passou na triagem. Recebe o texto completo do item.

Seis campos, todos ancorados no texto. Nada além disso na fase 1.

```
1. O que foi decidido (2 a 3 frases)
2. Fundamentação legal: artigos da Lei 14.133 citados no texto
3. Impacto prático para o fornecedor
4. O que muda no procedimento de quem presta consultoria
5. Nível de impacto: alto / medio / baixo
6. Resumo final de no máximo 200 palavras
```

**Regras anti-alucinação (obrigatórias no prompt):**

- Todo campo tem que sair do texto fornecido. Não havendo base, retorne `null`.
- **Proibido afirmar que o entendimento é novo, inédito, ou que muda
  jurisprudência anterior.** Não existe base histórica para sustentar isso na
  fase 1. Se o próprio texto da fonte disser que é mudança de entendimento, você
  pode reproduzir isso citando que a fonte afirma.
- Proibido citar artigo de lei que não apareça no texto.
- **Se não houver identificador citável E link, o item não entra no e-mail.**
  Vai para a tabela com status `sem_ancora` e fica registrado para revisão.
  "E" é literal: falta de qualquer um dos dois barra o item, não só a falta
  dos dois juntos (achado real: citações de TCU embutidas no informativo do
  TCE-MG têm número de acórdão mas nenhum link — barradas mesmo tendo
  número). "Identificador citável" é número de acórdão OU de processo — o
  que a fonte usar pra se citar (TCE-SP, STJ e TCE-MG decisão própria nunca
  citam por acórdão, só por processo; são a maioria das decisões relevantes
  do banco, e exigir acórdão ao pé da letra excluiria quase todo o
  boletim) — OU, quando a notícia é sobre licitação em andamento sem
  decisão julgada ainda, o número do próprio instrumento (Concorrência,
  Pregão, Edital, Resolução — achado real 2026-08-11, fonte Zênite: sem
  essa ampliação, notícia relevante sobre licitação em curso ficaria
  sem_ancora só por não existir acórdão pra citar, mesmo linkando pra fonte
  original e citando o número do próprio processo licitatório).

### Camada 6 — Boletim

Agrupado por tribunal, na ordem de prioridade: TCU, TCE-SP, STJ, TCE-MG, TCE-PR,
demais. Dentro de cada tribunal, ordenado por impacto (alto → baixo).

```
BOLETIM JURÍDICO NEXLICIT — 30/07/2026

Panorama: 4 decisões relevantes de 23 itens analisados.

━━━ TCU ━━━

[ALTO] Acórdão 1412/2026 — Plenário — Rel. Min. Augusto Nardes — 22/07/2026
Tema: qualificação técnica

<resumo de 200 palavras>

Artigos: 14.133 art. 67
→ Inteiro teor: <link>

━━━ TCE-PR ━━━
...
```

Cada item carrega: tribunal, número do acórdão, órgão julgador, relator, data,
tema, nível de impacto, resumo, artigos, e link para a **fonte original** (PDF do
boletim do TCU, notícia da Zênite, acórdão no ViaJuris). Não existe painel.

**Título de cada decisão no boletim:** nunca o título do documento-fonte
(`itens_brutos.titulo` é da publicação inteira — ex. "TCU Informativo LC
528/2026" repete pra todas as decisões daquela edição, não identifica uma
em particular). Montar um título específico por decisão:
`f"{tribunal} — Acórdão {numero_acordao}"`, com fallback pra
`numero_processo` quando não houver número de acórdão, seguido de um
trecho da ementa. Decidido na Etapa 5, antes de `nucleo/boletim.py`
existir, pra não se perder até a Etapa 7 — ver também
`DecisaoFatiada.identificador_exibicao` (Etapa 5), que já resolve um
identificador parecido (número + relator/data) por decisão, usado hoje só
nas tabelas de log/triagem.

### Camada 7 — Envio

Gmail SMTP, mesmo padrão do Radar. Credenciais em secrets do GitHub.
Se `len(decisoes_do_dia) == 0`, o job encerra com log e não envia nada.

---

## 4. Schema SQLite

Banco em branch `data` separada, para não poluir o histórico do portfólio.

```sql
CREATE TABLE fontes (
    id            INTEGER PRIMARY KEY,
    nome          TEXT NOT NULL UNIQUE,
    tipo          TEXT NOT NULL,        -- 'tribunal' | 'especializada'
    url_base      TEXT NOT NULL,
    prioridade    INTEGER NOT NULL,     -- ordem no boletim
    ativo         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE itens_brutos (
    id                INTEGER PRIMARY KEY,
    fonte_id          INTEGER NOT NULL REFERENCES fontes(id),
    url_origem        TEXT NOT NULL,
    titulo            TEXT,
    data_publicacao   TEXT,             -- ISO 8601
    texto_bruto       TEXT NOT NULL,
    hash_conteudo     TEXT NOT NULL,
    coletado_em       TEXT NOT NULL,
    status            TEXT NOT NULL     -- 'coletado'|'fatiado'|'erro'
);

CREATE UNIQUE INDEX idx_itens_hash ON itens_brutos(hash_conteudo);

CREATE TABLE decisoes (
    id                 INTEGER PRIMARY KEY,
    item_bruto_id      INTEGER NOT NULL REFERENCES itens_brutos(id),
    chave_dedup        TEXT NOT NULL,
    tribunal           TEXT NOT NULL,
    numero_acordao     TEXT,
    numero_processo    TEXT,
    orgao_julgador     TEXT,
    relator            TEXT,
    data_julgamento    TEXT,
    url_inteiro_teor   TEXT,
    tema               TEXT,
    artigos_lei        TEXT,            -- JSON array
    impacto            TEXT,            -- 'alto'|'medio'|'baixo'
    resumo             TEXT,
    triagem_status     TEXT NOT NULL,   -- 'relevante'|'descartado'|'sem_ancora'
    triagem_motivo     TEXT,
    analisado_em       TEXT,
    enviado_em         TEXT
);

CREATE UNIQUE INDEX idx_decisoes_dedup ON decisoes(chave_dedup);
CREATE INDEX idx_decisoes_envio ON decisoes(enviado_em, impacto);

CREATE TABLE execucoes (
    id                INTEGER PRIMARY KEY,
    iniciado_em       TEXT NOT NULL,
    finalizado_em     TEXT,
    itens_coletados   INTEGER DEFAULT 0,
    itens_triados     INTEGER DEFAULT 0,
    decisoes_enviadas INTEGER DEFAULT 0,
    email_enviado     INTEGER DEFAULT 0,
    erros             TEXT              -- JSON array
);
```

`chave_dedup` com índice único é o que impede decisão repetida. O `INSERT`
usa `ON CONFLICT DO NOTHING`.

A tabela `decisoes` guarda tudo, inclusive o que foi descartado na triagem. Isso
é o embrião da base histórica da fase 3 e permite auditar se a triagem está
jogando fora coisa boa.

---

## 5. Fases de implementação

Uma etapa por vez no Claude Code, com validação antes de avançar.

**Etapa 1 — Esqueleto e banco.** Estrutura de pastas, schema, camada de acesso
com sqlite3 puro e transação, seed da tabela `fontes`, testes.

**Etapa 2 — Um coletor só: Zênite.** É a fonte mais fácil e a de maior volume.
Testar `wp-json`, cair para RSS, cair para HTML. Rodar local e conferir se os
itens batem com o que aparece no site.

**Etapa 3 — Coletores de tribunal.** Nesta ordem de dificuldade crescente:
TCE-PR (HTML limpo), TCE-MG (HTML, achar o índice primeiro), TCE-SP, STJ, TCU
(o único com PDF).

**Etapa 4 — Fatiador.** Por fonte, com teste sobre um documento real de cada uma.

**Etapa 5 — Cliente LLM e triagem.** Reaproveitar a camada trocável do NexLicit
Engine. Rodar a triagem sobre uma semana de itens já coletados e conferir os
descartes na mão.

**Etapa 6 — Análise.** Só depois que a triagem estiver calibrada.

**Etapa 7 — Montador de e-mail e envio.**

**Etapa 8 — GitHub Actions.** Só no fim, quando tudo rodar local.

### Validação (o equivalente ao golden test do Engine)

Antes de ligar o cron: pegue duas semanas de publicações já conhecidas, rode o
pipeline inteiro e leia as decisões descartadas uma a uma. Se a triagem estiver
jogando fora coisa que você usaria com cliente, o prompt precisa afrouxar. Se
estiver deixando passar ruído, apertar.

---

## 6. Riscos conhecidos

**Limite do free tier do Gemini.** O volume estimado é de 15 a 30 itens/dia para
triagem e 3 a 6 para análise. Cabe folgado, mas implemente backoff exponencial e
processe em lote sequencial, não em paralelo, para não estourar requisições por
minuto.

**Fragilidade dos scrapers.** Cinco sites governamentais mudam layout sem aviso.
Cada coletor tem que falhar isolado, sem derrubar o job. O e-mail leva uma linha
de rodapé listando quais fontes falharam, igual ao WARNING do Radar. Coletor
silenciosamente quebrado por três semanas é pior do que coletor que grita.

**Periodicidade real.** TCU quinzenal, TCE-SP mensal, TCE-MG e STJ quinzenais,
TCE-PR semanal, Zênite diária. Isso significa e-mails magros na maioria dos dias
e alguns dias sem e-mail nenhum. É o comportamento esperado, não um defeito.

**Atraso de publicação.** O Boletim n.º 179/2025 do TCE-PR trata de sessões de
novembro de 2025. Boletim de jurisprudência não é notícia, é curadoria, e ela
sai defasada. Quem dá o tempo real é a Zênite.

**Backfill pendente do TCE-MG (edições anteriores à 332).** O HTML do
informativo do TCE-MG mudou de estrutura ao longo do tempo — pelo menos
três variantes reais já identificadas (2026-08-07). O fatiador (Etapa 5)
cobre as duas mais recentes (cabeçalho de seção com âncora vazia, e com
âncora envolvendo o texto em negrito). Edições anteriores à 332 usam um
sumário organizado por colegiado ("Tribunal Pleno", "Segunda Câmara") em
vez de por tribunal, e nem citam a decisão própria no formato "Processo
<a...>" que as edições recentes usam — cobrir isso exigiria uma terceira
extração inteira, não coberta agora. Esses itens ficam com
`itens_brutos.status = 'erro'`, registrados mas sem decisão extraída
(`fatiar_item` devolvendo lista vazia é tratado como falha, não sucesso
silencioso). Não afeta a coleta diária, que só processa edições novas no
formato atual — é só histórico incompleto. Se algum dia fizer sentido ter
o histórico completo do TCE-MG, é aqui que o trabalho fica pendente.

---

## 7. O que fica para a fase 3

Registrado aqui para não perder a ideia, mas fora do escopo agora:

- Detecção de contradição e de tendência, usando a base histórica acumulada
- Busca por tema e por artigo da 14.133
- Painel Streamlit em cima do mesmo SQLite
- Boletim como produto (muda tom, exige disclaimer e revisão humana antes do
  envio, e obriga a repensar a coleta na Zênite)
