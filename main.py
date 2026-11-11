import os
import re
import sqlite3
from datetime import datetime
import requests
from google.colab import userdata

# ==========================================
# CONFIGURAÇÕES E CONSTANTES
# ==========================================
# API de Consulta Pública do PNCP (Sem necessidade de Token/JWT para leitura)
API_BASE_URL = "https://pncp.gov.br/api/consulta"
DB_NAME = "licitacoes_geociencias.db"

# Códigos oficiais de Modalidades de Contratação do PNCP para monitoramento
MODALIDADES = {
    1: "Leilão",
    2: "Concorrência",
    3: "Concurso",
    4: "Pregão",
    5: "Diálogo Competitivo",
    6: "Dispensa de Licitação",
    7: "Inexigibilidade"
}

# Dicionário Regex expandido para Geologia, Hidrogeologia, Geotecnia, Meio Ambiente e Afins
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

# Configuração do Bot do Telegram usando o gerenciador de Secrets do Colab
try:
    TELEGRAM_TOKEN = userdata.get("TELEGRAM_TOKEN")
    TELEGRAM_CHAT_ID = userdata.get("TELEGRAM_CHAT_ID")
except Exception:
    TELEGRAM_TOKEN = ""
    TELEGRAM_CHAT_ID = ""
    print("[!] Atenção: Chaves TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não encontradas nos Secrets do Colab (ícone de chave 🔑).")

# ==========================================
# FASE 1 & 3: PERSISTÊNCIA (BANCO DE DADOS)
# ==========================================
def inicializar_banco():
    """Cria o banco de dados e a tabela para controle de duplicidade."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS licitacoes (
            id_pncp TEXT PRIMARY KEY,
            orgao TEXT,
            objeto TEXT,
            valor REAL,
            link TEXT,
            data_descoberta TEXT
        )
    """)
    conn.commit()
    conn.close()

def licitacao_ja_existe(id_pncp):
    """Verifica se o edital já foi coletado e alertado anteriormente."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM licitacoes WHERE id_pncp = ?", (id_pncp,))
    existe = cursor.fetchone() is not None
    conn.close()
    return existe

def salvar_licitacao(id_pncp, orgao, objeto, valor, link):
    """Registra a nova licitação para evitar envios duplicados no futuro."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO licitacoes (id_pncp, orgao, objeto, valor, link, data_descoberta)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (id_pncp, orgao, objeto, valor, link, datetime.now().isoformat()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()

# ==========================================
# FASE 4: ENTREGA (NOTIFICAÇÕES VIA TELEGRAM)
# ==========================================
def enviar_alerta_telegram(mensagem):
    """Envia o alerta formatado direto para o canal ou chat do Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        # Se as variáveis não estiverem configuradas, apenas ignora
        return

    # URL oficial da API de Bots do Telegram
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
            print(f"[!] Erro ao enviar para o Telegram. Resposta da API: {response.text}")
    except Exception as e:
        print(f"[!] Falha ao disparar mensagem para o Telegram: {e}")

# ==========================================
# FASE 2: EXECUÇÃO DO PIPELINE DE INGESTÃO e FILTRO
# ==========================================
def processar_oportunidades():
    """Varre o endpoint de propostas abertas do PNCP filtrando o escopo."""
    data_alvo = datetime.now().strftime("%Y%m%d")
    print(f"[*] Buscando contratações com propostas abertas até: {data_alvo}")

    # Endpoint do PNCP focado em oportunidades com recebimento de propostas em aberto
    endpoint = f"{API_BASE_URL}/v1/contratacoes/proposta"

    # API do PNCP exige paginação (limite máximo de 50 registros por página neste endpoint)
    pagina = 1
    total_relevantes = 0

    while True:
        params = {
            "dataFinal": data_alvo,
            "pagina": pagina,
            "tamanhoPagina": 50
        }

        try:
            response = requests.get(endpoint, params=params, timeout=15)

            if response.status_code == 204: # No Content: Fim dos registros
                break
            elif response.status_code != 200:
                print(f"[!] Erro na API do PNCP: Status {response.status_code}")
                break

            dados = response.json()
            items = dados.get("data", [])

            if not items:
                break

            for item in items:
                objeto = item.get("objeto", "")

                # Executa a filtragem cirúrgica usando Regex
                if objeto and REGEX_FILTRO.search(objeto):
                    # O PNCP gera IDs únicos compostos ou estruturados
                    id_pncp = f"{item.get('cnpjOrgao')}-{item.get('anoCompra')}-{item.get('sequencialCompra')}"

                    if licitacao_ja_existe(id_pncp):
                        continue

                    # Extração e tratamento de metadados
                    orgao = item.get("orgaoEntidade", {}).get("razaoSocial", "Órgão Não Identificado")
                    valor_estimado = item.get("valorTotalEstimado") or 0.0
                    link_edital = item.get("urlItemControle") or f"https://pncp.gov.br{item.get('cnpjOrgao')}/{item.get('anoCompra')}/{item.get('sequencialCompra')}"
                    modalidade_id = item.get("codigoModalidadeContratacao")
                    nome_modalidade = MODALIDADES.get(modalidade_id, f"Modalidade {modalidade_id}")

                    # Formatação do Alerta
                    msg_alerta = (
                        f"🚨 <b>NOVA OPORTUNIDADE ENCONTRADA!</b>\n\n"
                        f"🏛 <b>Órgão:</b> {orgao}\n"
                        f"🛠 <b>Modalidade:</b> {nome_modalidade}\n"
                        f"💰 <b>Valor Estimado:</b> R$ {valor_estimado:,.2f}\n\n"
                        f"📝 <b>Objeto:</b>\n{objeto}\n\n"
                        f"🔗 <a href='{link_edital}'>Clique aqui para acessar o Edital</a>"
                    )

                    # Exibe no terminal e envia para o Telegram
                    print(f"\n[+] Alerta gerado para ID {id_pncp}: {orgao}")
                    print(f"    Objeto resumido: {objeto[:90]}...")

                    enviar_alerta_telegram(msg_alerta)
                    salvar_licitacao(id_pncp, orgao, objeto, valor_estimado, link_edital)
                    total_relevantes += 1

            # Controle de paginação baseado nos metadados do PNCP
            paginas_restantes = dados.get("paginasRestantes", 0)
            if paginas_restantes == 0:
                break
            pagina += 1

        except Exception as e:
            print(f"[!] Erro crítico durante a varredura da página {pagina}: {e}")
            break

    print(f"\n[✓] Varredura concluída. {total_relevantes} novas licitações adicionadas ao banco.")

# ==========================================
# EXECUÇÃO PRINCIPAL
# ==========================================
if __name__ == "__main__":
    inicializar_banco()
    processar_oportunidades()
