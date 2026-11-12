"""
Monitor de licitações de Geociências no PNCP com alertas no Telegram.

Funciona no GitHub Actions (variáveis de ambiente) e no Google Colab (userdata).
"""
import html
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import requests

API_BASE_URL = "https://pncp.gov.br/api/consulta"
DB_NAME = os.getenv("DB_NAME", "licitacoes_geociencias.db")

# Janela de busca: contratações com recebimento de propostas aberto até N dias à frente
DIAS_A_FRENTE = int(os.getenv("DIAS_A_FRENTE", "60"))
TAMANHO_PAGINA = 50          # máximo aceito pelo endpoint /proposta
MAX_PAGINAS = int(os.getenv("MAX_PAGINAS", "400"))
# Limite de alertas por execução (evita "enxurrada" na 1ª execução; o restante vai no dia seguinte)
MAX_ALERTAS = int(os.getenv("MAX_ALERTAS", "80"))

REGEX_FILTRO = re.compile(
    r"\b("
    r"geologia|ge[oó]logos?|geol[oó]gic[oa]s?|geotecnia|geot[eé]cnic[oa]s?|"
    r"hidrogeologia|hidrogeol[oó]gic[oa]s?|geof[ií]sica|geof[ií]sic[oa]s?|"
    r"po[cç]os?\s+artesianos?|perfura[cç][aã]o\s+de\s+po[cç]os?|po[cç]os?\s+de\s+monitoramento|"
    r"sondage(m|ns)\s+(a\s+trado|rotativas?|spt|mistas?|(a|à|de)\s+percuss[aã]o|percuss[aã]o)|"
    r"estabilidade\s+de\s+taludes?|conten[cç][aã]o\s+de\s+encostas?|mapeamento\s+geol[oó]gico|"
    r"ensaios?\s+de\s+solos?|mec[aâ]nica\s+dos\s+solos|outorga\s+de\s+(uso\s+de\s+)?[aá]gua|"
    r"rebaixamento\s+de\s+len[cç]ol|"
    r"topografia|topogr[aá]fic[oa]s?|georreferenciamento|georreferenciad[oa]s?|aerofotogrametria|"
    r"meio\s+ambiente|ambiental|licenciamento\s+ambiental|estudo\s+de\s+impacto|eia[\s/-]*rima|"
    r"recupera[cç][aã]o\s+de\s+[aá]reas?\s+degradadas?|prad|barragem|barragens|"
    r"recursos\s+h[ií]dricos|[aá]guas\s+subterr[aâ]neas|testes?\s+de\s+bombeamento"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- credenciais
def carregar_credenciais():
    """Tenta o Colab (userdata); se não estiver no Colab, usa variáveis de ambiente."""
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not (token and chat_id):
        try:
            from google.colab import userdata  # só existe no Colab
            token = token or userdata.get("TELEGRAM_TOKEN")
            chat_id = chat_id or userdata.get("TELEGRAM_CHAT_ID")
        except Exception:
            pass
    return (token or "").strip(), str(chat_id or "").strip()


TELEGRAM_TOKEN, TELEGRAM_CHAT_ID = carregar_credenciais()


# ---------------------------------------------------------------- banco
def inicializar_banco():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS licitacoes ("
            "id_pncp TEXT PRIMARY KEY, orgao TEXT, objeto TEXT, valor REAL, "
            "link TEXT, data_descoberta TEXT)"
        )


def licitacao_ja_existe(id_pncp):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute(
            "SELECT 1 FROM licitacoes WHERE id_pncp = ?", (id_pncp,)
        ).fetchone() is not None


def salvar_licitacao(id_pncp, orgao, objeto, valor, link):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO licitacoes VALUES (?, ?, ?, ?, ?, ?)",
            (id_pncp, orgao, objeto, valor, link, datetime.now().isoformat()),
        )


# ---------------------------------------------------------------- telegram
def enviar_alerta(mensagem):
    """Envia mensagem ao Telegram. Retorna True só se o Telegram confirmou o envio."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] TELEGRAM_TOKEN/TELEGRAM_CHAT_ID não configurados.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for tentativa in range(4):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200 and r.json().get("ok"):
                time.sleep(1.1)  # respeita o limite de ~1 msg/s por chat
                return True
            if r.status_code == 429:
                espera = r.json().get("parameters", {}).get("retry_after", 5)
                print(f"[!] Telegram limitou envios; aguardando {espera}s")
                time.sleep(espera + 1)
                continue
            print(f"[!] Telegram respondeu {r.status_code}: {r.text[:300]}")
            if r.status_code in (400, 401, 403, 404):
                return False  # token/chat inválido ou mensagem malformada: não adianta repetir
        except requests.RequestException as e:
            print(f"[!] Erro envio Telegram (tentativa {tentativa + 1}): {e}")
        time.sleep(3)
    return False


# ---------------------------------------------------------------- formatação
def formatar_moeda(valor):
    try:
        v = float(valor or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0:
        return "Não informado / sigiloso"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_data(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso[:19]).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return iso


def montar_mensagem(item, link):
    esc = lambda s: html.escape(str(s or ""), quote=True)
    orgao = (item.get("orgaoEntidade") or {}).get("razaoSocial") or "Órgão Não Identificado"
    unidade = item.get("unidadeOrgao") or {}
    local = " / ".join(x for x in (unidade.get("municipioNome"), unidade.get("ufSigla")) if x) or "—"
    objeto = (item.get("objetoCompra") or "").strip()
    if len(objeto) > 2500:
        objeto = objeto[:2500] + "…"
    return (
        "🚨 <b>OPORTUNIDADE GEOCIÊNCIAS!</b>\n\n"
        f"🏛 <b>Órgão:</b> {esc(orgao)}\n"
        f"📍 <b>Local:</b> {esc(local)}\n"
        f"🛠 <b>Modalidade:</b> {esc(item.get('modalidadeNome') or 'Outros')}\n"
        f"💰 <b>Valor estimado:</b> {esc(formatar_moeda(item.get('valorTotalEstimado')))}\n"
        f"⏰ <b>Propostas até:</b> {esc(formatar_data(item.get('dataEncerramentoProposta')))}\n"
        f"📝 <b>Objeto:</b>\n{esc(objeto)}\n\n"
        f"🔗 <a href=\"{esc(link)}\">Acesse o Edital no PNCP</a>"
    )


# ---------------------------------------------------------------- PNCP
def buscar_pagina(pagina, data_final):
    """Retorna o JSON de uma página (dict) ou None se não houver dados."""
    endpoint = f"{API_BASE_URL}/v1/contratacoes/proposta"
    params = {"dataFinal": data_final, "pagina": pagina, "tamanhoPagina": TAMANHO_PAGINA}
    headers = {"User-Agent": "monitor-licitacoes-geociencias/2.0", "Accept": "application/json"}
    for tentativa in range(5):
        try:
            r = requests.get(endpoint, params=params, headers=headers, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 204:
                return None
            print(f"[!] PNCP página {pagina}: HTTP {r.status_code} {r.text[:200]}")
        except (requests.RequestException, ValueError) as e:
            print(f"[!] PNCP página {pagina} (tentativa {tentativa + 1}): {e}")
        time.sleep(2 ** tentativa * 2)  # 2, 4, 8, 16, 32 s
    raise RuntimeError(f"PNCP não respondeu na página {pagina} após 5 tentativas")


def processar_oportunidades():
    inicializar_banco()
    data_final = (datetime.now() + timedelta(days=DIAS_A_FRENTE)).strftime("%Y%m%d")
    print(f"[*] Varrendo contratações com propostas abertas no PNCP (dataFinal={data_final})...")

    analisados = encontrados = enviados = falhas = 0
    pagina, total_paginas = 1, 1
    erro_pncp = None

    while pagina <= total_paginas and pagina <= MAX_PAGINAS:
        try:
            dados = buscar_pagina(pagina, data_final)
        except RuntimeError as e:
            erro_pncp = str(e)
            print(f"[!] {e}")
            break
        if not dados:
            break
        total_paginas = int(dados.get("totalPaginas") or 1)
        itens = dados.get("data") or []
        print(f"[*] Página {pagina}/{total_paginas}: {len(itens)} itens")

        for item in itens:
            analisados += 1
            objeto = item.get("objetoCompra") or ""
            if not REGEX_FILTRO.search(objeto):
                continue

            org = item.get("orgaoEntidade") or {}
            cnpj = org.get("cnpj") or "00000000000000"
            ano = item.get("anoCompra") or datetime.now().year
            seq = item.get("sequencialCompra") or 0
            id_pncp = item.get("numeroControlePNCP") or f"{cnpj}-{ano}-{seq}"
            if licitacao_ja_existe(id_pncp):
                continue
            encontrados += 1
            if enviados >= MAX_ALERTAS:
                continue  # fica para a próxima execução (não é salvo no banco)

            link = f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}"
            orgao = org.get("razaoSocial") or "Órgão Não Identificado"
            print(f"[+] Enviando alerta: {orgao} | {objeto[:80]}")
            if enviar_alerta(montar_mensagem(item, link)):
                enviados += 1
                salvar_licitacao(id_pncp, orgao, objeto, item.get("valorTotalEstimado") or 0.0, link)
            else:
                falhas += 1
                if falhas >= 3 and enviados == 0:
                    print("[!] Telegram falhou 3 vezes seguidas. Verifique TOKEN e CHAT_ID.")
                    return 1
        pagina += 1

    pendentes = encontrados - enviados
    resumo = (
        f"📊 <b>Varredura PNCP concluída</b> ({datetime.now():%d/%m/%Y %H:%M})\n"
        f"Licitações analisadas: {analisados}\n"
        f"Novas oportunidades enviadas: {enviados}"
        + (f"\nPendentes para a próxima execução: {pendentes}" if pendentes > 0 else "")
        + (f"\n⚠️ Varredura incompleta: {html.escape(erro_pncp)}" if erro_pncp else "")
    )
    print(resumo.replace("<b>", "").replace("</b>", ""))
    enviar_alerta(resumo)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return 1
    return 1 if (erro_pncp and analisados == 0) else 0


if __name__ == "__main__":
    sys.exit(processar_oportunidades())
