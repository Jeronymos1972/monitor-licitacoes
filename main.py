import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
import requests

API_BASE_URL = "https://pncp.gov.br/api/consulta"
DB_NAME = "licitacoes_geociencias.db"

MODALIDADES = {
    1: "Leilão", 2: "Concorrência", 3: "Concurso", 4: "Pregão",
    5: "Diálogo Competitivo", 6: "Dispensa de Licitação", 7: "Inexigibilidade"
}

# Filtro consolidado de Geociências
REGEX_FILTRO = re.compile(
    r"\b("
    r"geologia|ge[oó]logo|geol[oó]gic[oa]s?|geotecnia|geot[eé]cnico|hidrogeologia|hidrogeol[oó]gico|geof[ií]sica|geof[ií]sico|"
    r"poço\s+artesiano|poços\s+artesianos|perfura[cç][aã]o\s+de\s+poço|poço\s+de\s+monitoramento|"
    r"sondagem\s+(a\s+trado|rotativa|spt|mista|percuss[aã]o)|"
    r"estabilidade\s+de\s+talude|conten[cç][aã]o\s+de\s+encosta|mapeamento\s+geol[oó]gico|"
    r"ensaio\s+de\s+solo|mecanica\s+dos\s+solos|outorga\s+de\s+agua|rebaixamento\s+de\s+len[cç]ol|"
    r"topografia|topogr[aá]fico|georreferenciamento|georreferenciado|aerofotogrametria|"
    r"meio\s+ambiente|ambiental|licenciamento\s+ambiental|estudo\s+de\s+impacto|eia\s+rima|"
    r"recupera[cç][aã]o\s+de\s+área\s+degradada|prad|barragem|barragens|recursos\s+h[ií]dricos|"
    r"águas\s+subterrâneas|teste\s+de\s+bombeamento"
    r")\b",
    re.IGNORECASE
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    try:
        from google.colab import userdata
        TELEGRAM_TOKEN = userdata.get("TELEGRAM_TOKEN")
        TELEGRAM_CHAT_ID = userdata.get("TELEGRAM_CHAT_ID")
    except Exception:
        pass

def inicializar_banco():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS licitacoes (id_pncp TEXT PRIMARY KEY, orgao TEXT, objeto TEXT, valor REAL, link TEXT, data_descoberta TEXT)")
    conn.commit()
    conn.close()

def licitacao_ja_existe(id_pncp):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM licitacoes WHERE id_pncp = ?", (id_pncp,))
    existe = cursor.fetchone() is not None
    conn.close()
    return existe

def salvar_licitacao(id_pncp, orgao, objeto, valor, link):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO licitacoes (id_pncp, orgao, objeto, valor, link, data_descoberta) VALUES (?, ?, ?, ?, ?, ?)", (id_pncp, orgao, objeto, valor, link, datetime.now().isoformat()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()

def enviar_alerta_telegram(mensagem):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Telegram não configurado. Pulando envio.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"[!] Erro na resposta do Telegram: {response.text}")
    except Exception as e:
        print(f"[!] Erro de conexão com o Telegram: {e}")

def processar_oportunidades():
    # Janela de 3 dias futuros para evitar lentidão e timeout no servidor do PNCP
    data_futura = (datetime.now() + timedelta(days=3)).strftime("%Y%m%d")
    print(f"[*] Buscando propostas de contratação ativas até: {data_futura}")
    endpoint = f"{API_BASE_URL}/v1/contratacoes/proposta"
    pagina = 1
    total_novos = 0

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://pncp.gov.br/app/editais"
    }

    while pagina <= 3:
        params = {"dataFinal": data_futura, "pagina": pagina, "tamanhoPagina": 30}
        sucesso = False
        
        # Mecanismo de re-tentativas robusto contra lentidão no PNCP
        for tentativa in range(3):
            try:
                response = requests.get(endpoint, params=params, headers=headers, timeout=40)
                if response.status_code == 200:
                    sucesso = True
                    break
                elif response.status_code == 204:
                    break
                else:
                    time.sleep(3)
            except Exception:
                time.sleep(3)

        if not sucesso:
            break

        try:
            dados = response.json()
            items = dados.get("data", [])
            if not items:
                break

            for item in items:
                if not item:
                    continue
                try:
                    objeto = item.get("objeto", "")
                    if objeto and REGEX_FILTRO.search(objeto):
                        cnpj_orgao = item.get('cnpjOrgao') or '00000000000000'
                        ano_compra = item.get('anoCompra') or datetime.now().year
                        seq_compra = item.get('sequencialCompra') or '0'
                        id_pncp = f"{cnpj_orgao}-{ano_compra}-{seq_compra}"

                        if licitacao_ja_existe(id_pncp):
                            continue

                        orgao_entidade = item.get("orgaoEntidade")
                        orgao = orgao_entidade.get("razaoSocial", "Órgão Não Identificado") if isinstance(orgao_entidade, dict) else "Órgão Não Identificado"

                        valor_estimado = item.get("valorTotalEstimado") or 0.0
                        link_edital = item.get("urlItemControle") or f"https://pncp.gov.br/{cnpj_orgao}/{ano_compra}/{seq_compra}"
                        nome_modalidade = MODALIDADES.get(item.get("codigoModalidadeContratacao"), "Outros")

                        msg_alerta = (
                            f"🚨 <b>NOVA OPORTUNIDADE ENCONTRADA!</b>\n\n"
                            f"🏛 <b>Órgão:</b> {orgao}\n"
                            f"🛠 <b>Modalidade:</b> {nome_modalidade}\n"
                            f"💰 <b>Valor Estimado:</b> R$ {valor_estimado:,.2f}\n\n"
                            f"📝 <b>Objeto:</b>\n{objeto}\n\n"
                            f"🔗 <a href='{link_edital}'>Clique aqui para acessar o Edital</a>"
                        )
                        print(f"[+] Notificando oportunidade: {id_pncp} - {orgao}")
                        enviar_alerta_telegram(msg_alerta)
                        salvar_licitacao(id_pncp, orgao, objeto, valor_estimado, link_edital)
                        total_novos += 1
                except Exception as item_err:
                    continue

            if dados.get("paginasRestantes", 0) == 0:
                break
            pagina += 1
        except Exception:
            break
            
    print(f"[✓] Processamento concluído. {total_novos} novos registros encontrados.")

if __name__ == '__main__':
    inicializar_banco()
    processar_oportunidades()