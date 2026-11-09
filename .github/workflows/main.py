import os
import re
import sqlite3
from datetime import datetime
import requests

API_BASE_URL = "https://pncp.gov.br/api/consulta"
DB_NAME = "licitacoes_geociencias.db"

MODALIDADES = {
    1: "Leilão", 2: "Concorrência", 3: "Concurso", 4: "Pregão",
    5: "Diálogo Competitivo", 6: "Dispensa de Licitação", 7: "Inexigibilidade"
}

REGEX_FILTRO = re.compile(
    r"\b("
    r"geotecnia|geot[eé]cnico|hidrogeologia|hidrogeol[oó]gico|geof[ií]sica|geof[ií]sico|"
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
    # Criando tabela com string simples de linha única para evitar conflitos no editor
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
        print("[!] Credenciais do Telegram ausentes. Pulando envio de mensagem.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"[!] Erro na resposta do Telegram: {response.text}")
    except Exception as e:
        print(f"[!] Erro de conexão com o Telegram: {e}")

def processar_oportunidades():
    data_alvo = datetime.now().strftime("%Y%m%d")
    print(f"[*] Buscando contratações ativas do PNCP até: {data_alvo}")
    endpoint = f"{API_BASE_URL}/v1/contratacoes/proposta"
    pagina = 1
    total_novos = 0

    while True:
        params = {"dataFinal": data_alvo, "pagina": pagina, "tamanhoPagina": 50}
        try:
            response = requests.get(endpoint, params=params, timeout=15)
            if response.status_code == 204 or response.status_code != 200:
                break

            dados = response.json()
            items = dados.get("data", [])
            if not items:
                break

            for item in items:
                objeto = item.get("objeto", "")
                if objeto and REGEX_FILTRO.search(objeto):
                    id_pncp = f"{item.get('cnpjOrgao')}-{item.get('anoCompra')}-{item.get('sequencialCompra')}"

                    if licitacao_ja_existe(id_pncp):
                        continue

                    orgao = item.get("orgaoEntidade", {}).get("razaoSocial", "Órgão Não Identificado")
                    valor_estimado = item.get("valorTotalEstimado") or 0.0
                    link_edital = item.get("urlItemControle") or f"https://pncp.gov.br{item.get('cnpjOrgao')}/{item.get('anoCompra')}/{item.get('sequencialCompra')}"
                    nome_modalidade = MODALIDADES.get(item.get("codigoModalidadeContratacao"), "Outros")

                    msg_alerta = (
                        f"🚨 <b>NOVA OPORTUNIDADE ENCONTRADA!</b>\n\n"
                        f"🏛 <b>Órgão:</b> {orgao}\n"
                        f"🛠 <b>Modalidade:</b> {nome_modalidade}\n"
                        f"💰 <b>Valor:</b> R$ {valor_estimado:,.2f}\n\n"
                        f"📝 <b>Objeto:</b>\n{objeto}\n\n"
                        f"🔗 <a href='{link_edital}'>Acesse o Edital</a>"
                    )
                    print(f"[+] Notificando oportunidade: {id_pncp}")
                    enviar_alerta_telegram(msg_alerta)
                    salvar_licitacao(id_pncp, orgao, objeto, valor_estimado, link_edital)
                    total_novos += 1

            if dados.get("paginasRestantes", 0) == 0:
                break
            pagina += 1
        except Exception as e:
            print(f"[!] Erro durante execução: {e}")
            break
    print(f"[✓] Processamento concluído. {total_novos} novos registros encontrados.")

if __name__ == '__main__':
    inicializar_banco()
    processar_oportunidades()