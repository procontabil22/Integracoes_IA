# ============================================================
#  Integracoes_IA — Microserviço Proxy IA  v2.0.0
#  Suporte: Anthropic (Claude Sonnet 4.6) + OpenAI (GPT-4o)
#  Reforma Tributária · IBS/CBS · NCM · NBS · LC 214/2025
#  Deploy: GitHub + Railway
#
#  COMPATÍVEL COM: INTEGRACAO_FRONTEND_v2.ts
#  Rotas consumidas pelo Next.js (nunca pelo browser):
#    POST /classificar          → /api/ia/classificar/route.ts
#    POST /classificar/lote     → /api/ia/classificar-lote/route.ts
#    POST /testar-conexao       → /api/ia/testar-conexao/route.ts
#    GET  /cache/stats          → /api/ia/cache/route.ts  (GET)
#    POST /cache/limpar         → /api/ia/cache/route.ts  (POST)
#    GET  /health               → /api/ia/status/route.ts
# ============================================================

import os
import json
import time
import hashlib
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Literal, List

import httpx
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()

# ── Versão ───────────────────────────────────────────────────
VERSION = "2.0.0"

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("integracoes_ia")

# ── Rate Limiter ─────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

# ── App ──────────────────────────────────────────────────────
app = FastAPI(
    title="Integracoes_IA",
    description=(
        "Microserviço proxy seguro para Anthropic (Claude) e OpenAI.\n"
        "Reforma Tributária IBS/CBS · NCM · NBS · LC 214/2025 · EC 132/2023\n\n"
        "**Atenção:** todas as rotas protegidas exigem o header `X-API-Secret`.\n"
        "O caller correto é o servidor Next.js — nunca o browser diretamente."
    ),
    version=VERSION,
    docs_url="/docs",
    redoc_url="/redoc"
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ─────────────────────────────────────────────────────
# Em produção: ALLOWED_ORIGINS deve conter APENAS a URL do seu Next.js no Vercel.
# O browser nunca chega aqui diretamente — apenas o servidor Next.js chega.
# Logo, a origem que o Railway vê é o IP/domínio do Vercel (server-side).
# Para server-to-server no Vercel, pode usar "*" ou a URL exata do deploy.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:3001").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Secret"],
)

# ── Cache em memória (TTL configurável) ───────────────────────
_cache: dict = {}
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "86400"))  # 24h padrão


def cache_get(key: str):
    if key in _cache:
        entry = _cache[key]
        if time.time() - entry["ts"] < CACHE_TTL:
            logger.info(f"[CACHE HIT] {key[:32]}...")
            return entry["data"]
        del _cache[key]
    return None


def cache_set(key: str, data: dict):
    _cache[key] = {"data": data, "ts": time.time()}
    logger.info(f"[CACHE SET] total={len(_cache)} entradas")


def make_cache_key(descricao: str, tipo: str, provedor: str) -> str:
    raw = f"{descricao.strip().lower()}|{tipo}|{provedor}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Autenticação por secret header ───────────────────────────
API_SECRET = os.getenv("API_SECRET", "")


def verificar_secret(request: Request):
    """
    Valida o header X-API-Secret.
    Em desenvolvimento (ENVIRONMENT=development) sem secret configurado, libera.
    Em produção, secret ausente no servidor é erro de configuração (500).
    """
    if not API_SECRET:
        if os.getenv("ENVIRONMENT", "production") == "development":
            return
        raise HTTPException(
            status_code=500,
            detail="API_SECRET não configurado no servidor"
        )
    secret = request.headers.get("X-API-Secret", "")
    if secret != API_SECRET:
        raise HTTPException(
            status_code=401,
            detail="Não autorizado — header X-API-Secret inválido ou ausente"
        )


# ══════════════════════════════════════════════════════════════
# SCHEMAS  (espelham os tipos em src/types/integracoes.types.ts)
# ══════════════════════════════════════════════════════════════

class ClassificarRequest(BaseModel):
    descricao: str = Field(..., min_length=2, max_length=500,
                           description="Descrição do produto ou serviço")
    tipo: Literal["produto", "servico"] = Field(...,
                                                description="'produto' para NCM ou 'servico' para NBS")
    provedor: Optional[Literal["anthropic", "openai"]] = Field(
        None,
        description="Provedor de IA. Se omitido, usa PROVEDOR_ATIVO do servidor."
    )


class TestarConexaoRequest(BaseModel):
    provedor: Literal["anthropic", "openai"]
    # api_key opcional: permite testar key nova sem salvar no servidor.
    # O frontend v2 não envia api_key por padrão (segurança extra).
    api_key: Optional[str] = Field(None, description="Key temporária para teste. Omita para usar a do servidor.")


# ══════════════════════════════════════════════════════════════
# PROMPTS
# ══════════════════════════════════════════════════════════════

def prompt_produto(produto: str) -> str:
    return f"""Você é especialista em tributação brasileira na Reforma Tributária (EC 132/2023 e LC 214/2025).

Produto: "{produto}"

REGRA CRÍTICA — NCMs COMPARTILHADOS: Vários NCMs abrangem produtos distintos com cClassTrib
e tratamentos tributários completamente diferentes. Exemplos reais obrigatórios:
- NCM 9619.00.00: absorventes (cClassTrib 200013, redução 100%) vs fraldas (cClassTrib 200035, redução 60%)
- NCM 2201.10.00: água com gás vs água sem gás (regimes monofásicos distintos)
- NCM 3004.90.99: medicamentos genéricos vs referência (cClassTrib distintos)
- NCM 0402.10.10: leite em pó integral vs desnatado
Se o NCM for compartilhado, preencha OBRIGATORIAMENTE "variantesNCM" com TODOS os produtos
que usam esse NCM e têm tributações DISTINTAS.

Retorne SOMENTE JSON válido sem markdown:
{{
  "produto": "nome padronizado",
  "ncm": "XXXX.XX.XX",
  "descricaoNCM": "descrição oficial da posição NCM",
  "ncmCompartilhado": true,
  "ncmObservacao": "por que esse NCM é compartilhado e quais produtos o utilizam",
  "cst": "100",
  "descricaoCST": "Tributação integral",
  "classeIBS": "A",
  "descricaoClasse": "descrição da classe IBS",
  "cClassTrib": "000001",
  "cClassTribDescricao": "000001 — grupo tributário — regime — art. X LC 214/2025",
  "cClassTribGrupo": "nome do grupo tributário",
  "segmento": "segmento econômico",
  "tributacaoIntegral": true,
  "reducaoBC": "0%",
  "pRedIBS": 0,
  "pRedCBS": 0,
  "regimeEspecial": null,
  "aliqIBS": "14.7%",
  "aliqCBS": "8.8%",
  "aliqTotal": "26.5%",
  "aliqReduzidaIBS": "14.7%",
  "aliqReduzidaCBS": "8.8%",
  "aliqEfetivaTotal": "26.5%",
  "isencao": false,
  "monofasico": false,
  "impostoseletivo": false,
  "aliqIS": null,
  "observacoes": "observações tributárias relevantes",
  "variantesNCM": [
    {{
      "produto": "nome do produto variante que usa o mesmo NCM",
      "descricaoDiferenca": "motivo pelo qual tem tratamento diferente",
      "cst": "200",
      "cClassTrib": "200013",
      "cClassTribDescricao": "200013 — desc completa — art. X LC 214/2025",
      "reducaoBC": "100%",
      "aliqReduzidaIBS": "0%",
      "aliqReduzidaCBS": "0%",
      "aliqEfetivaTotal": "0%",
      "isencao": true,
      "fundamentoLegal": "art. X, Anexo Y, LC 214/2025"
    }}
  ],
  "fundamentacaoLegal": [
    {{"diploma": "EC 132/2023", "artigo": "art. X", "ementa": "dispositivo constitucional"}},
    {{"diploma": "LC 214/2025", "artigo": "art. X, § Y, inciso Z", "ementa": "descrição do dispositivo para este produto"}}
  ]
}}

Regras obrigatórias:
- CST 3 dígitos: 100=integral, 200=redução alíquota, 220=redução BC, 300=isento/alíquota zero,
  400=imune, 500=monofásico, 510=monofásico integral, 620=monofásico diferimento,
  700=suspensão, 900=não incidência
- cClassTrib 6 dígitos (IT 2025.002): 000001=padrão integral, 200013=absorventes 100%,
  200035=fraldas 60%, 300xxx=cesta básica, 410xxx=monofásico, 500xxx=saúde/medicamentos,
  600xxx=educação, 700xxx=agropecuário, 800xxx=imune/isento, 900xxx=Imposto Seletivo
- Alíquotas referência: IBS=14,7%, CBS=8,8%, total=26,5%
- Se ncmCompartilhado=false retorne variantesNCM:[]
- Fundamentação mínima obrigatória: EC 132/2023 + LC 214/2025 + artigos específicos do produto"""


def prompt_servico(servico: str) -> str:
    return f"""Você é especialista em tributação de serviços na Reforma Tributária brasileira
(EC 132/2023, LC 214/2025, LC 116/2003).

Serviço: "{servico}"

Retorne SOMENTE JSON válido sem markdown:
{{
  "servico": "nome padronizado do serviço",
  "tipo": "Serviço",
  "nbs": "X.XXXX.XX.XX",
  "descricaoNBS": "descrição oficial da NBS 2.0",
  "itemLC116": "XX.XX",
  "descricaoLC116": "descrição do item na lista LC 116/2003",
  "cIndOp": "1",
  "descricaoCIndOp": "prestação onerosa nacional",
  "localIncidencia": "Domicílio do adquirente — justificativa legal",
  "cst": "100",
  "descricaoCST": "Tributação integral",
  "classeIBS": "A",
  "descricaoClasse": "descrição da classe IBS",
  "cClassTrib": "000001",
  "cClassTribDescricao": "000001 — grupo tributário — regime — art. X LC 214/2025",
  "cClassTribGrupo": "nome do grupo tributário",
  "segmento": "segmento econômico",
  "tributacaoIntegral": true,
  "reducaoBC": "0%",
  "pRedIBS": 0,
  "pRedCBS": 0,
  "aliqIBS": "14.7%",
  "aliqCBS": "8.8%",
  "aliqTotal": "26.5%",
  "aliqReduzidaIBS": "14.7%",
  "aliqReduzidaCBS": "8.8%",
  "aliqEfetivaTotal": "26.5%",
  "isencao": false,
  "regimeTransicao": {{
    "aliqISSvigente": "2% a 5%",
    "municipioReferencia": "varia por município",
    "vigenciaISS": "até 31/12/2032",
    "vigenciaIBSCBS": "a partir de 01/01/2026",
    "coexistencia": "descrição detalhada de como ISS e IBS/CBS coexistem no período de transição para este serviço específico",
    "nfseObrigatoria": true
  }},
  "observacoes": "observações tributárias relevantes",
  "fundamentacaoLegal": [
    {{"diploma": "EC 132/2023", "artigo": "art. X", "ementa": "dispositivo constitucional"}},
    {{"diploma": "LC 214/2025", "artigo": "art. X, § Y", "ementa": "descrição do dispositivo"}},
    {{"diploma": "LC 116/2003", "artigo": "item XX.XX", "ementa": "descrição do item ISS vigente até 2032"}}
  ]
}}

Regras obrigatórias:
- NBS 2.0 conforme Portaria Conjunta RFB/SECEX nº 2.000/2018
- LC 116/2003: manter referência para o período de transição até 31/12/2032
- cIndOp: 1=prestação onerosa nacional, 2=importação de serviço,
  3=exportação de serviço, 4=não incide IBS/CBS
- Local de incidência: regra geral = domicílio do adquirente (LC 214/2025)
- ISS: alíquota mínima 2% (Lei 157/2016), máxima 5% (LC 116/2003)
- Reduções LC 214/2025: educação 60%, saúde 60%, transporte coletivo 60%
- Serviços financeiros e seguros: regime diferenciado (art. 183 a 200 LC 214/2025)
- NFS-e nacional: obrigatória a partir de janeiro/2026
- Fundamentação mínima: EC 132/2023 + LC 214/2025 + LC 116/2003 + artigos específicos"""


# ══════════════════════════════════════════════════════════════
# CHAMADAS ÀS APIs
# ══════════════════════════════════════════════════════════════

async def chamar_anthropic(prompt: str, api_key: str = None) -> dict:
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    if not key:
        raise HTTPException(status_code=401, detail="API Key Anthropic não configurada")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": model,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}]
            }
        )

    if resp.status_code != 200:
        err = resp.json()
        msg = err.get("error", {}).get("message", f"HTTP {resp.status_code}")
        raise HTTPException(status_code=resp.status_code, detail=f"Anthropic: {msg}")

    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []))
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


async def chamar_openai(prompt: str, api_key: str = None) -> dict:
    key = api_key or os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    if not key:
        raise HTTPException(status_code=401, detail="API Key OpenAI não configurada")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}"
            },
            json={
                "model": model,
                "max_tokens": 2000,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": "Especialista tributação brasileira Reforma Tributária LC 214/2025. Retorne SEMPRE JSON válido sem markdown."
                    },
                    {"role": "user", "content": prompt}
                ]
            }
        )

    if resp.status_code != 200:
        err = resp.json()
        msg = err.get("error", {}).get("message", f"HTTP {resp.status_code}")
        raise HTTPException(status_code=resp.status_code, detail=f"OpenAI: {msg}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


async def classificar_com_fallback(prompt: str, provedor: str) -> tuple:
    """Tenta provedor principal; se falhar, tenta o outro automaticamente."""
    outro = "openai" if provedor == "anthropic" else "anthropic"
    provedores = [provedor]

    chave_outro = "OPENAI_API_KEY" if outro == "openai" else "ANTHROPIC_API_KEY"
    if os.getenv(chave_outro):
        provedores.append(outro)

    ultimo_erro = None
    for p in provedores:
        try:
            logger.info(f"[IA] Chamando {p}...")
            fn = chamar_anthropic if p == "anthropic" else chamar_openai
            resultado = await fn(prompt)
            return resultado, p
        except Exception as e:
            logger.warning(f"[FALHA {p}] {e}")
            ultimo_erro = e

    raise HTTPException(
        status_code=502,
        detail=f"Todos os provedores falharam. Último erro: {ultimo_erro}"
    )


# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.get("/health", tags=["Status"])
async def health():
    """
    Status do serviço. Público — sem X-API-Secret.
    Consumido por: /api/ia/status/route.ts
    """
    return {
        "status": "ok",
        "servico": "Integracoes_IA",
        "versao": VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cache_entradas": len(_cache),
        "provedores": {
            "anthropic": {
                "configurado": bool(os.getenv("ANTHROPIC_API_KEY")),
                "modelo": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
            },
            "openai": {
                "configurado": bool(os.getenv("OPENAI_API_KEY")),
                "modelo": os.getenv("OPENAI_MODEL", "gpt-4o")
            }
        },
        "provedor_ativo": os.getenv("PROVEDOR_ATIVO", "anthropic")
    }
    # Nota v2: "origens_permitidas" removido do health — não expor config interna


@app.post("/classificar", tags=["Tributário"], dependencies=[Depends(verificar_secret)])
@limiter.limit("30/minute")
async def classificar(request: Request, body: ClassificarRequest):
    """
    Classifica um produto ou serviço conforme LC 214/2025.
    Exige header: X-API-Secret
    Consumido por: /api/ia/classificar/route.ts

    Resposta espelha RespostaClassificacao (integracoes.types.ts):
      { sucesso, provedor, fallback?, tipo, resultado, cache }
    """
    provedor = body.provedor or os.getenv("PROVEDOR_ATIVO", "anthropic")
    cache_key = make_cache_key(body.descricao, body.tipo, provedor)

    cached = cache_get(cache_key)
    if cached:
        return {
            "sucesso": True,
            "provedor": provedor,
            "tipo": body.tipo,
            "resultado": cached,
            "cache": True
        }

    prompt = prompt_produto(body.descricao) if body.tipo == "produto" else prompt_servico(body.descricao)
    resultado, provedor_usado = await classificar_com_fallback(prompt, provedor)
    cache_set(cache_key, resultado)

    logger.info(f"[OK] tipo={body.tipo} provedor={provedor_usado} desc={body.descricao[:50]}")

    return {
        "sucesso": True,
        "provedor": provedor_usado,
        "fallback": provedor_usado != provedor,
        "tipo": body.tipo,
        "resultado": resultado,
        "cache": False
    }


@app.post("/classificar/lote", tags=["Tributário"], dependencies=[Depends(verificar_secret)])
@limiter.limit("5/minute")
async def classificar_lote(request: Request, body: List[ClassificarRequest]):
    """
    Classifica múltiplos itens (máx. 50).
    Delay de 500ms entre requisições para respeitar rate limits das APIs.
    Itens em cache retornam sem nova chamada à IA (custo zero).
    Exige header: X-API-Secret
    Consumido por: /api/ia/classificar-lote/route.ts

    Resposta espelha RespostaLote (integracoes.types.ts):
      { total, sucesso, erros, do_cache, chamadas_ia, resultados[] }
    """
    if len(body) > 50:
        raise HTTPException(status_code=400, detail="Máximo 50 itens por lote")

    resultados = []

    for i, item in enumerate(body):
        provedor = item.provedor or os.getenv("PROVEDOR_ATIVO", "anthropic")
        cache_key = make_cache_key(item.descricao, item.tipo, provedor)
        cached = cache_get(cache_key)

        if cached:
            resultados.append({
                "indice": i,
                "descricao": item.descricao,
                "tipo": item.tipo,
                "sucesso": True,
                "provedor": provedor,
                "resultado": cached,
                "cache": True
            })
            continue

        prompt = prompt_produto(item.descricao) if item.tipo == "produto" else prompt_servico(item.descricao)

        try:
            resultado, provedor_usado = await classificar_com_fallback(prompt, provedor)
            cache_set(cache_key, resultado)
            resultados.append({
                "indice": i,
                "descricao": item.descricao,
                "tipo": item.tipo,
                "sucesso": True,
                "provedor": provedor_usado,
                "fallback": provedor_usado != provedor,
                "resultado": resultado,
                "cache": False
            })
        except Exception as e:
            logger.error(f"[ERRO lote idx={i}] {e}")
            resultados.append({
                "indice": i,
                "descricao": item.descricao,
                "tipo": item.tipo,
                "sucesso": False,
                "erro": str(e)
            })

        if i < len(body) - 1:
            await asyncio.sleep(0.5)

    ok       = sum(1 for r in resultados if r.get("sucesso"))
    do_cache = sum(1 for r in resultados if r.get("cache"))

    return {
        "total":       len(body),
        "sucesso":     ok,
        "erros":       len(body) - ok,
        "do_cache":    do_cache,
        "chamadas_ia": ok - do_cache,
        "resultados":  resultados
    }


@app.post("/testar-conexao", tags=["Configuração"], dependencies=[Depends(verificar_secret)])
@limiter.limit("10/minute")
async def testar_conexao(request: Request, body: TestarConexaoRequest):
    """
    Testa se a API Key é válida.
    Exige header: X-API-Secret
    Consumido por: /api/ia/testar-conexao/route.ts

    O frontend v2 não envia api_key por padrão — testa a key do servidor.
    api_key opcional disponível para uso administrativo direto via Swagger.

    Resposta espelha TesteConexao (integracoes.types.ts):
      { status, provedor, modelo?, mensagem }
    """
    try:
        if body.provedor == "anthropic":
            key = body.api_key or os.getenv("ANTHROPIC_API_KEY")
            if not key:
                return JSONResponse(
                    status_code=400,
                    content={"status": "erro", "provedor": "anthropic", "mensagem": "API Key Anthropic não informada"}
                )
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01"
                    },
                    json={
                        "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                        "max_tokens": 5,
                        "messages": [{"role": "user", "content": "ok"}]
                    }
                )
            modelo = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
            if resp.status_code == 200:
                return {
                    "status": "ok",
                    "provedor": "anthropic",
                    "modelo": modelo,
                    "mensagem": f"✓ Conexão com Anthropic bem-sucedida! Modelo: {modelo}"
                }
            err = resp.json()
            msg = err.get("error", {}).get("message", f"HTTP {resp.status_code}")
            return JSONResponse(
                status_code=400,
                content={"status": "erro", "provedor": "anthropic", "mensagem": msg}
            )

        elif body.provedor == "openai":
            key = body.api_key or os.getenv("OPENAI_API_KEY")
            if not key:
                return JSONResponse(
                    status_code=400,
                    content={"status": "erro", "provedor": "openai", "mensagem": "API Key OpenAI não informada"}
                )
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"}
                )
            modelo = os.getenv("OPENAI_MODEL", "gpt-4o")
            if resp.status_code == 200:
                return {
                    "status": "ok",
                    "provedor": "openai",
                    "modelo": modelo,
                    "mensagem": f"✓ Conexão com OpenAI bem-sucedida! Modelo: {modelo}"
                }
            err = resp.json()
            msg = err.get("error", {}).get("message", f"HTTP {resp.status_code}")
            return JSONResponse(
                status_code=400,
                content={"status": "erro", "provedor": "openai", "mensagem": msg}
            )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "erro", "provedor": body.provedor, "mensagem": str(e)}
        )


@app.post("/cache/limpar", tags=["Configuração"], dependencies=[Depends(verificar_secret)])
async def limpar_cache():
    """
    Limpa todo o cache em memória.
    Exige header: X-API-Secret
    Consumido por: /api/ia/cache/route.ts (POST { acao: "limpar" })
    """
    count = len(_cache)
    _cache.clear()
    return {
        "mensagem": f"Cache limpo com sucesso. {count} entradas removidas.",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.get("/cache/stats", tags=["Configuração"], dependencies=[Depends(verificar_secret)])
async def cache_stats():
    """
    Estatísticas do cache em memória.
    Exige header: X-API-Secret
    Consumido por: /api/ia/cache/route.ts (GET)

    Resposta espelha StatsCache (integracoes.types.ts):
      { total, validas, expiradas, ttl_horas }
    """
    agora = time.time()
    validas = sum(1 for v in _cache.values() if agora - v["ts"] < CACHE_TTL)
    return {
        "total":     len(_cache),
        "validas":   validas,
        "expiradas": len(_cache) - validas,
        "ttl_horas": round(CACHE_TTL / 3600, 1)
    }


# ── Startup log ───────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    logger.info("=" * 60)
    logger.info(f"  Integracoes_IA v{VERSION} — Iniciado")
    logger.info(f"  Anthropic : {'✓' if os.getenv('ANTHROPIC_API_KEY') else '✗ não configurado'}"
                f" | modelo: {os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-6')}")
    logger.info(f"  OpenAI    : {'✓' if os.getenv('OPENAI_API_KEY') else '✗ não configurado'}"
                f" | modelo: {os.getenv('OPENAI_MODEL', 'gpt-4o')}")
    logger.info(f"  Provedor  : {os.getenv('PROVEDOR_ATIVO', 'anthropic')}")
    logger.info(f"  Secret    : {'✓ configurado' if API_SECRET else '✗ AUSENTE — risco de segurança!'}")
    logger.info(f"  Cache TTL : {round(CACHE_TTL / 3600, 1)}h")
    logger.info(f"  CORS      : {', '.join(ALLOWED_ORIGINS)}")
    logger.info(f"  Docs      : /docs  |  /redoc")
    logger.info("=" * 60)


# ── Entry point local ─────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "3333")),
        reload=True
    )
