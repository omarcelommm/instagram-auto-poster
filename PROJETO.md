# Instagram Auto Poster — Documentação do Projeto

## O que é

Sistema automatizado que posta Reels no Instagram 3x por dia sem intervenção manual.

O fluxo completo: busca um vídeo aleatório no Google Drive → transcreve o áudio → gera legenda com IA → faz upload → publica no Instagram → registra no log → notifica por push.

---

## Infraestrutura

| Componente | Tecnologia | Função |
|---|---|---|
| Backend | FastAPI + Python | API REST que controla o sistema |
| Hospedagem | Railway | Roda o backend 24/7 |
| Agendamento | cron-job.org | Chama `POST /post/now` nos horários definidos |
| Vídeos | Google Drive | Fonte de vídeos brutos |
| Transcrição | OpenAI Whisper | Extrai o texto do áudio do vídeo |
| Legenda | Claude Haiku | Gera legenda otimizada para Instagram |
| Upload público | Cloudinary | Hospeda o vídeo para a API do Instagram acessar |
| Postagem | Instagram Graph API v22 | Publica como Reel |
| Notificações | ntfy.sh (tópico: `marcelo-social-media-alerts`) | Push no celular em sucesso/falha |
| Frontend | React + Recharts + Tailwind (Lovable) | Dashboard de acompanhamento |

**Repositórios:**
- Backend: `github.com/omarcelommm/instagram-auto-poster`
- Frontend: `github.com/omarcelommm/neon-grid-autoposter`

**URL da API em produção:** `https://web-production-42c88.up.railway.app`

**URL do painel:** Lovable (sincroniza automaticamente com o GitHub do frontend)

---

## Horários de postagem

Configurados no cron-job.org com chamada `POST /post/now`:

- 07:15 (horário de Brasília)
- 12:15
- 20:15

---

## Variáveis de ambiente (Railway)

| Variável | Descrição |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | JSON da service account do Google em **base64** (importante: base64, não JSON puro) |
| `GOOGLE_DRIVE_FOLDER_ID` | ID da pasta no Drive com os vídeos |
| `META_ACCESS_TOKEN` | Token da Graph API do Instagram |
| `INSTAGRAM_ACCOUNT_ID` | ID da conta Instagram Business |
| `OPENAI_API_KEY` | Para o Whisper (transcrição) |
| `ANTHROPIC_API_KEY` | Para o Claude Haiku (legenda) |
| `CLOUDINARY_CLOUD_NAME` | Credenciais do Cloudinary |
| `CLOUDINARY_API_KEY` | |
| `CLOUDINARY_API_SECRET` | |

> **Atenção:** `GOOGLE_SERVICE_ACCOUNT_JSON` deve ser armazenado como base64. Para gerar:
> ```bash
> base64 -i service_account.json | tr -d '\n' | pbcopy
> ```

---

## Estrutura de arquivos

```
social_media/
├── api.py                  # FastAPI — endpoints do painel
├── postar_instagram.py     # Lógica principal de postagem
├── posted_videos.json      # Log de vídeos já postados (salvo no Railway — resetado a cada redeploy)
├── service_account.json    # Credencial Google Drive (local apenas, não commitada)
├── Procfile                # Comando de start no Railway
├── pyproject.toml          # Dependências Python
├── PROJETO.md              # Este documento
└── videos/                 # Pasta local de vídeos (não usada em produção)
```

---

## Endpoints da API

| Método | Rota | Descrição |
|---|---|---|
| GET | `/status` | Total de vídeos, postados, restantes, último post |
| GET | `/posts` | Histórico de posts (do mais recente ao mais antigo) |
| GET | `/queue` | Lista de vídeos no Drive ainda não postados |
| GET | `/analytics` | Posts com métricas do Instagram (plays, curtidas, comentários, salvamentos, alcance) |
| GET | `/posts/{post_id}/insights` | Insights de um post específico |
| POST | `/post/now` | Dispara uma postagem imediata (roda em background) |
| GET | `/post/status` | Se tem postagem em andamento e qual foi o último resultado |
| GET | `/schedule` | Horários do cron local (obsoleto — cron agora é no cron-job.org) |

---

## Fluxo de postagem (passo a passo)

1. cron-job.org chama `POST /post/now` no horário configurado
2. `selecionar_video()` — lista vídeos no Drive, filtra os já postados, escolhe um aleatório
3. `baixar_video_drive()` — baixa o vídeo para pasta temporária no Railway
4. `transcrever()` — extrai áudio via ffmpeg, envia para Whisper, retorna texto
5. `gerar_legenda()` — envia transcrição para Claude Haiku, retorna legenda formatada
6. `comprimir_video()` — comprime se o arquivo for maior que 80MB
7. `fazer_upload_publico()` — sobe para Cloudinary, retorna URL pública
8. `criar_container()` — cria container de mídia no Instagram (tipo REELS)
9. `aguardar_processamento()` — polling a cada 15s até status `FINISHED` (máx 20 tentativas)
10. `publicar()` — publica o container, retorna `post_id`
11. `salvar_postado()` — registra no `posted_videos.json`
12. ntfy.sh — envia push de sucesso ou falha

---

## Painel (Frontend)

**Dashboard:**
- Cards: Total de vídeos, Postados, Restantes, Último Post
- Botão "Postar Agora" (com polling de status)
- Gráfico de área: posts por dia nos últimos 30 dias

**Posts:**
- Seção "Fila" (suspensa/colapsável): vídeos no Drive ainda não postados
- Seção "Postados": histórico com arquivo, data, legenda e link direto para o Instagram

**Análises:**
- Engajamento por post (plays, curtidas, comentários, salvamentos) — gráfico de barras agrupadas
- Melhor horário para postar — média de engajamento por hora
- Melhor dia da semana — média por dia
- Tabela comparativa com todos os posts e métricas

> As métricas de engajamento dependem da permissão `instagram_manage_insights` no token Meta. Já liberada.

---

## Permissões Meta (Instagram Graph API)

Permissões ativas no app `Social-media-IG`:
- `instagram_business_basic`
- `instagram_manage_comments`
- `instagram_business_manage_messages`
- `instagram_manage_insights` ← adicionada para o painel de Analytics
- `pages_read_engagement`

---

## Notificações (ntfy.sh)

App instalado no celular com inscrição no tópico `marcelo-social-media-alerts`.

- **Sucesso:** título "✓ Instagram — Post publicado", mostra nome do arquivo e quantos restam
- **Falha:** título "✗ Instagram — Falha na postagem", prioridade alta, mostra o erro

---

## Limitação importante: log efêmero

O `posted_videos.json` é salvo no filesystem do Railway, que é **resetado a cada redeploy**. Isso significa que se o Railway redeployar (ex: novo commit, reinicialização), o histórico de quais vídeos já foram postados é perdido e o sistema pode repetir vídeos.

**Solução futura:** persistir o log em banco externo (Supabase, Railway Postgres ou similar).

---

## Ideias futuras

### Alta prioridade

- **Persistir log em banco de dados externo**
  O `posted_videos.json` é apagado a cada redeploy do Railway. Migrar para PostgreSQL (Railway tem plano gratuito) ou Supabase garante que nenhum vídeo seja repetido mesmo após reinicializações.

- **Renovação automática do token Meta**
  O `META_ACCESS_TOKEN` expira. Implementar rotina de renovação automática usando o token de longa duração (Long-Lived Token) e salvando o novo valor via Railway API.

### Melhorias do painel

- **Ordem da fila controlável**
  Hoje o vídeo é escolhido aleatoriamente. Adicionar opção de ordenar a fila manualmente (drag-and-drop) para controlar qual vídeo sai em qual horário.

- **Preview do próximo vídeo**
  Mostrar no dashboard qual será o próximo vídeo a ser postado (se a fila for ordenada).

- **Editar legenda antes de postar**
  Botão "Postar Agora" que abre um modal com a legenda gerada para revisão antes de publicar.

- **Histórico de legendas geradas**
  Guardar todas as legendas geradas (mesmo as de posts futuros) para reutilizar ou adaptar.

### Inteligência

- **Otimização automática de horário**
  Com dados acumulados de engajamento, o sistema sugere (ou ajusta automaticamente) os melhores horários para postar baseado em desempenho histórico.

- **A/B de legendas**
  Gerar duas versões de legenda e rotacionar entre elas para identificar qual estilo performa melhor.

- **Classificação automática de vídeos**
  Antes de postar, categorizar o vídeo por tema (posicionamento, precificação, case, etc.) e balancear a distribuição de conteúdo ao longo da semana.

### Operacional

- **Webhook de falha com retry automático**
  Se a postagem falhar, tentar novamente após 30 minutos antes de notificar.

- **Relatório semanal por email/WhatsApp**
  Todo domingo, enviar resumo: posts da semana, métricas de engajamento, crescimento de seguidores.

- **Multi-conta**
  Suportar mais de um perfil do Instagram com configurações separadas de horário e fonte de vídeos.
