import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
import requests
from google.colab import userdata

API_BASE_URL = "https://pncp.gov.br/api/consulta"
DB_NAME = "licitacoes_geociencias.db"

MODALIDADES = {
    1: "Leilão", 2: "Concorrência", 3: "Concurso", 4: "Pregão",
    5: "Diálogo Competitivo", 6: "Dispensa de Licitação", 7: "Inexigibilidade"
}

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

try:
    TELEGRAM_TOKEN = userdata.get("TELEGRAM_TOKEN")
    TELEGRAM_CHAT_ID = userdata.get("TELEGRAM_CHAT_ID")
except:
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

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
    res = cursor.fetchone() is not None
    conn.close()
    return res

def salvar_licitacao(id_pncp, orgao, objeto, valor, link):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO licitacoes VALUES (?, ?, ?, ?, ?, ?)", (id_pncp, orgao, objeto, valor, link, datetime.now().isoformat()))
        conn.commit()
    except:
        pass
    finally:
        conn.close()

def enviar_alerta(mensagem):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mensagem, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print(f"[!] Erro envio Telegram: {e}")

def processar_oportunidades():
    inicializar_banco()
    # Varre de 3 dias atrás até 3 dias à frente de forma leve para evitar timeouts no PNCP
    data_inicio = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")
    data_fim = (datetime.now() + timedelta(days=3)).strftime("%Y%m%d")
    print(f"[*] Varrendo propostas ativas no PNCP entre {data_inicio} e {data_fim}...")

    endpoint = f"{API_BASE_URL}/v1/contratacoes/proposta"
    headers = {"User-Agent": "Mozilla/5.0"}

    for data_alvo in [data_inicio, data_fim]:
        params = {"dataFinal": data_alvo, "pagina": 1, "tamanhoPagina": 50}
        for tentativa in range(3):
            try:
                res = requests.get(endpoint, params=params, headers=headers, timeout=40)
                if res.status_code == 200:
                    itens = res.json().get("data", [])
                    for item in itens:
                        obj = item.get("objeto", "")
                        if obj and REGEX_FILTRO.search(obj):
                            cnpj = item.get('cnpjOrgao', '00000000000000')
                            ano = item.get('anoCompra', datetime.now().year)
                            seq = item.get('sequencialCompra', '0')
                            id_pncp = f"{cnpj}-{ano}-{seq}"

                            if licitacao_ja_existe(id_pncp):
                                continue

                            orgao = item.get("orgaoEntidade", {}).get("razaoSocial", "Órgão Não Identificado")
                            valor = item.get("valorTotalEstimado") or 0.0
                            link = item.get("urlItemControle") or f"https://pncp.gov.br/{cnpj}/{ano}/{seq}"
                            mod = MODALIDADES.get(item.get("codigoModalidadeContratacao"), "Outros")

                            msg = f"🚨 <b>OPORTUNIDADE GEOCIÊNCIAS!</b>\n\n🏛 <b>Órgão:</b> {orgao}\n🛠 <b>Modalidade:</b> {mod}\n💰 <b>Valor:</b> R$ {valor:,.2f}\n📝 <b>Objeto:</b>\n{obj}\n🔗 <a href='{link}'>Acesse o Edital</a>"
                            print(f"[+] Enviando alerta Telegram: {orgao}")
                            enviar_alerta(msg)
                            salvar_licitacao(id_pncp, orgao, obj, valor, link)
                    break
                elif res.status_code == 204:
                    break
            except Exception as e:
                print(f"[!] Erro tentativa {tentativa+1}: {e}")
                time.sleep(2)

if __name__ == '__main__':
    processar_oportunidades()
