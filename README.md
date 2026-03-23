# Integracoes_IA v2.0.0

Microserviço proxy seguro para **Anthropic (Claude Sonnet 4.6)** e **OpenAI (GPT-4o)**,
especializado em classificação tributária — Reforma Tributária brasileira (LC 214/2025 · EC 132/2023).

---

## Arquitetura de segurança

```
Browser (usuário)
    │
    │  Chama apenas /api/ia/* (Next.js interno)
    ▼
Servidor Next.js  ←── IA_PROXY_SECRET fica AQUI, nunca vai ao browser
    │
    │  POST https://integracoes-ia.railway.app/*
    │  Header: X-API-Secret: ***
    ▼
Microserviço Railway  ←── ANTHROPIC_API_KEY e OPENAI_API_KEY ficam AQUI
    │
    ├──▶ api.anthropic.com
    └──▶ api.openai.com
```

---

## Endpoints

| Método | Rota | Auth | Consumido por |
|--------|------|------|--------------|
| `GET` | `/health` | público | `/api/ia/status/route.ts` |
| `POST` | `/classificar` | X-API-Secret | `/api/ia/classificar/route.ts` |
| `POST` | `/classificar/lote` | X-API-Secret | `/api/ia/classificar-lote/route.ts` |
| `POST` | `/testar-conexao` | X-API-Secret | `/api/ia/testar-conexao/route.ts` |
| `GET` | `/cache/stats` | X-API-Secret | `/api/ia/cache/route.ts` (GET) |
| `POST` | `/cache/limpar` | X-API-Secret | `/api/ia/cache/route.ts` (POST) |

Documentação interativa: `https://sua-url.railway.app/docs`

---

## Deploy no Railway

### 1. Prepare o repositório

```bash
git init
git add .
git commit -m "feat: Integracoes_IA v2.0.0"
git remote add origin https://github.com/seu-usuario/Integracoes_IA.git
git push -u origin main
```

### 2. Crie o projeto no Railway

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
2. Selecione o repositório `Integracoes_IA`
3. Railway detecta o `railway.json` automaticamente

### 3. Configure as variáveis de ambiente

No painel Railway → **Variables**, adicione:

```
PORT=3333
ENVIRONMENT=production
API_SECRET=<gere com: python -c "import secrets; print(secrets.token_hex(32))">
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
ALLOWED_ORIGINS=https://seuapp.vercel.app
ANTHROPIC_MODEL=claude-sonnet-4-6
OPENAI_MODEL=gpt-4o
PROVEDOR_ATIVO=anthropic
CACHE_TTL_SECONDS=86400
```

### 4. Valide o deploy

```bash
# Health check (público)
curl https://integracoes-ia.railway.app/health

# Teste de conexão (requer secret)
curl -X POST https://integracoes-ia.railway.app/testar-conexao \
  -H "Content-Type: application/json" \
  -H "X-API-Secret: sua_chave" \
  -d '{"provedor": "anthropic"}'
```

---

## Configuração do Next.js (.env.local)

```env
# SEM NEXT_PUBLIC_ — ficam apenas no servidor Next.js
IA_PROXY_URL=https://integracoes-ia.railway.app
IA_PROXY_SECRET=mesmo_valor_do_API_SECRET_acima
```

---

## Desenvolvimento local

```bash
# Clone e instale
git clone https://github.com/seu-usuario/Integracoes_IA.git
cd Integracoes_IA
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Configure variáveis
cp env.example .env
# edite .env com suas keys reais

# Rode
python main.py
# Acesse: http://localhost:3333/docs
```

---

## Funcionalidades

- **Fallback automático**: se Anthropic falhar, tenta OpenAI automaticamente (e vice-versa)
- **Cache em memória**: classificações repetidas retornam instantaneamente (custo zero)
- **Rate limiting**: 30 req/min em `/classificar`, 5 req/min em `/classificar/lote`
- **Lote**: até 50 itens com delay de 500ms entre chamadas
- **OpenAI `response_format`**: força JSON nativo no GPT-4o (menos erros de parse)

---

## Compatibilidade

| Componente | Versão |
|-----------|--------|
| Este microserviço | v2.0.0 |
| INTEGRACAO_FRONTEND | v2 |
| Claude Sonnet | 4.6 (`claude-sonnet-4-6`) |
| GPT | 4o (`gpt-4o`) |
| Python | 3.11+ |
| FastAPI | 0.111.0 |
